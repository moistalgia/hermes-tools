#!/usr/bin/env python3
"""
Plex MCP server - a single-file, dependency-light bridge between an MCP client
(Hermes/GLADYS) and a Plex Media Server via python-plexapi.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python plex_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python plex_mcp_server.py list_players
         python plex_mcp_server.py search query="ready player one"
         python plex_mcp_server.py play query="ready player one" player="Theater"

Both paths run the exact same functions through the exact same argument
handling, so anything that works on the CLI works over MCP. If it breaks,
it breaks identically in both, which is the whole point.

Environment:
    PLEX_URL    default http://127.0.0.1:32400, which is right when Plex runs on
                the same host as Hermes. Give a LAN address if it does not.
    PLEX_TOKEN  required
    PLEX_PROXY  default 1 - route player commands through the Plex server
                instead of connecting to the player's LAN IP directly. Leave it
                on unless a device is only reachable directly.
"""

import datetime
import difflib
import inspect
import json
import os
import random
import re
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# PLEX_BASEURL is accepted because that is what plexapi calls it and what most
# MCP config examples use; PLEX_URL wins if both are set.
PLEX_URL = (
    os.environ.get("PLEX_URL")
    or os.environ.get("PLEX_BASEURL")
    or "http://127.0.0.1:32400"
)
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_PROXY = os.environ.get("PLEX_PROXY", "1") not in ("0", "false", "False", "")
PLEX_TIMEOUT = int(os.environ.get("PLEX_TIMEOUT", "15"))

# {"theater": "Streaming Stick 4K", "office": "unknown"} - maps what people say
# out loud onto what Plex calls the device. Plex names are frequently useless
# ("unknown", "Sleepy"), and a room name outlives the hardware in it.
try:
    PLEX_ALIASES = {
        str(k).strip().lower(): str(v)
        for k, v in json.loads(os.environ.get("PLEX_ALIASES", "{}")).items()
    }
except (ValueError, AttributeError):
    PLEX_ALIASES = {}
    print("[plex-mcp] PLEX_ALIASES is not valid JSON; ignoring it", file=sys.stderr)

# Roku's External Control Protocol. Open on the LAN, no auth, and answers even
# when the Plex app is closed - which is what lets us wake a device instead of
# telling the user to go press buttons.
ROKU_ECP_PORT = 8060
ROKU_PLEX_CHANNEL_ID = os.environ.get("ROKU_PLEX_CHANNEL_ID", "13535")

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "plex"
SERVER_VERSION = "1.2.0"


def log(msg):
    """Diagnostics go to stderr. stdout is reserved for JSON-RPC framing."""
    print(f"[plex-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_server = None


def plex():
    """Connect lazily and cache. Raises with an actionable message."""
    global _server
    if _server is not None:
        return _server
    if not PLEX_TOKEN:
        raise RuntimeError(
            "PLEX_TOKEN is not set in the environment. It is not on disk - "
            "set it in the MCP server config or the shell before starting."
        )
    try:
        from plexapi.server import PlexServer
    except ImportError:
        raise RuntimeError(
            "python-plexapi is not installed. Run: pip install plexapi"
        )
    _server = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT)
    log(f"connected to {_server.friendlyName} at {PLEX_URL}")
    return _server


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {}


def tool(description, schema=None, required=None):
    """Register a function as an MCP tool and a CLI subcommand."""

    def decorator(fn):
        props = schema or {}
        TOOLS[fn.__name__] = {
            "fn": fn,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required or [],
                "additionalProperties": False,
            },
        }
        return fn

    return decorator


def s(desc, default=None):
    d = {"type": "string", "description": desc}
    if default is not None:
        d["default"] = default
    return d


def i(desc, default=None):
    d = {"type": "integer", "description": desc}
    if default is not None:
        d["default"] = default
    return d


def b(desc, default=False):
    return {"type": "boolean", "description": desc, "default": default}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def text(value, default=""):
    """Coerce an argument that is meant to be a string.

    Arguments like resolution="1080" and decade="1990" arrive as integers both
    from the CLI's numeric coercion and from models that see a number and send
    one. Every one of those used to be an AttributeError on .strip().
    """
    if value is None:
        return default
    return str(value)


def ms_to_clock(ms):
    if not ms:
        return "0:00"
    total = int(ms // 1000)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def describe_item(item, detailed=False):
    """Flatten a Plex media object into something an LLM can reason about.

    `detailed` costs one extra request per item and is for when the agent is
    reasoning *about* media (recommending, comparing). Plain playback replies do
    not need a plot synopsis, and ten of them is a few thousand wasted tokens.
    """
    kind = getattr(item, "type", None)
    out = {
        "rating_key": str(getattr(item, "ratingKey", "")),
        "type": kind,
        "title": getattr(item, "title", None),
        "year": getattr(item, "year", None),
        "duration": ms_to_clock(getattr(item, "duration", None)),
        "library": getattr(item, "librarySectionTitle", None),
        "watched": bool(getattr(item, "viewCount", 0) or 0),
    }
    if kind == "episode":
        out["show"] = getattr(item, "grandparentTitle", None)
        out["season"] = getattr(item, "parentIndex", None)
        out["episode"] = getattr(item, "index", None)
        out["label"] = (
            f"{out['show']} S{out['season']:02d}E{out['episode']:02d} - {out['title']}"
            if out["season"] is not None and out["episode"] is not None
            else out["title"]
        )
    elif kind == "track":
        out["artist"] = getattr(item, "grandparentTitle", None)
        out["album"] = getattr(item, "parentTitle", None)
        out["label"] = f"{out['artist']} - {out['title']}"
    else:
        out["label"] = f"{out['title']} ({out['year']})" if out["year"] else out["title"]
    if detailed:
        # Plex truncates tag lists on listing endpoints - a search result shows
        # only the first two genres. Anything reporting genres has to reload or
        # it will state confidently that a Fantasy film is not Fantasy. Callers
        # working in bulk should pre-enrich with enrich_items() instead; this
        # reload is one HTTP request per item.
        if not getattr(item, "_hermes_enriched", False):
            try:
                item.reload()
            except Exception:
                pass
        out["genres"] = [g.tag for g in (getattr(item, "genres", None) or [])]
        out["directors"] = [d.tag for d in (getattr(item, "directors", None) or [])][:3]
        out["rating"] = getattr(item, "rating", None)
        out["audience_rating"] = getattr(item, "audienceRating", None)
        out["content_rating"] = getattr(item, "contentRating", None)
        summary = getattr(item, "summary", None)
        if summary:
            out["summary"] = summary[:400]
    return out


# ---------------------------------------------------------------------------
# Bulk library access
#
# The thing that used to make whole-library questions impossible: every listing
# tool capped out at a couple of dozen rows, so "what am I missing" turned into
# hundreds of sliced calls and the agent ran out of budget before it ran out of
# library.
#
# Two facts make the whole library cheap, and both were measured against a real
# 501-movie / 1850-episode server rather than assumed:
#
#   1. plexapi already walks X-Plex-Container-Start/Size internally. One
#      section.search(maxresults=None) returns every row in ~1s. There was never
#      a 25-item limit in Plex - that was ours.
#   2. Listing rows truncate tag lists to two entries, so genres off a listing
#      are wrong. Re-fetching /library/metadata/<k1,k2,...,k100> restores full
#      tags at 100 items per request: six parallel requests for 501 movies,
#      ~4s, versus the ~500 requests a per-item reload() would cost.
#
# So the expensive part is not talking to Plex, it is the tokens spent printing
# what comes back. That is what `detail` is for - see project_item.
# ---------------------------------------------------------------------------

LIBRARY_CACHE_TTL = 120
METADATA_BATCH = 100
METADATA_WORKERS = 4

_library_cache = {}


def invalidate_library_cache():
    _library_cache.clear()


def enrich_items(items):
    """Restore untruncated tag metadata on a list of items, in batches.

    Order is preserved. A batch that fails degrades to its truncated listing
    rows rather than failing the call - a partial genre list is worth more than
    an error, and the caller is told it happened.
    """
    p = plex()
    keys = []
    for item in items:
        try:
            keys.append(int(item.ratingKey))
        except (TypeError, ValueError):
            pass
    if not keys:
        return items, 0

    chunks = [keys[n:n + METADATA_BATCH] for n in range(0, len(keys), METADATA_BATCH)]
    failed = 0

    def fetch(chunk):
        try:
            return p.fetchItems(chunk)
        except Exception as exc:
            log(f"metadata batch of {len(chunk)} failed ({type(exc).__name__}: {exc})")
            return None

    by_key = {}
    with ThreadPoolExecutor(max_workers=METADATA_WORKERS) as pool:
        for got in pool.map(fetch, chunks):
            if got is None:
                failed += 1
                continue
            for item in got:
                item._hermes_enriched = True
                by_key[str(item.ratingKey)] = item

    return [by_key.get(str(x.ratingKey), x) for x in items], failed * METADATA_BATCH


def resolve_sections(library=None, media_type=None):
    """Sections matching a name and/or a media type, or every section."""
    p = plex()
    sections = list(p.library.sections())
    if library:
        want = text(library).strip().lower()
        matched = [x for x in sections if want in x.title.lower()]
        if not matched:
            raise ToolError(
                f"No library matches {library!r}.",
                available=[x.title for x in sections],
            )
        sections = matched
    if media_type:
        want = {"movie": "movie", "show": "show", "episode": "show",
                "season": "show", "artist": "artist", "album": "artist",
                "track": "artist"}.get(text(media_type).strip().lower())
        if want:
            sections = [x for x in sections if x.type == want]
    return sections


def section_items(section, libtype=None, enriched=True):
    """Every item in a section. Cached briefly - several tools want this list
    and pulling it three times in one turn is pure latency."""
    cache_key = (section.key, libtype, enriched)
    hit = _library_cache.get(cache_key)
    now = time.time()
    if hit and now - hit["at"] < LIBRARY_CACHE_TTL:
        return hit["items"], hit["degraded"]

    items = section.search(libtype=libtype, maxresults=None)
    degraded = 0
    if enriched and items:
        items, degraded = enrich_items(items)
    _library_cache[cache_key] = {"at": now, "items": items, "degraded": degraded}
    return items, degraded


# How much of each item to print. The whole library at "full" is a six-figure
# token bill and blows the context that was supposed to receive the answer;
# at "minimal" a 500-title inventory is a few thousand tokens. Whole-library
# reasoning wants minimal or compact, and the agent can then pull "full" for
# the handful of items it actually cares about.
DETAIL_LEVELS = ("minimal", "compact", "full")


def project_item(item, detail="compact"):
    """A token-budgeted view of one item. Null fields are dropped."""
    kind = getattr(item, "type", None)
    out = {
        "rating_key": str(getattr(item, "ratingKey", "")),
        "title": getattr(item, "title", None),
        "year": getattr(item, "year", None),
    }
    if kind == "episode":
        out["show"] = getattr(item, "grandparentTitle", None)
        out["season"] = getattr(item, "parentIndex", None)
        out["episode"] = getattr(item, "index", None)
    elif kind not in ("movie", None):
        out["type"] = kind

    if detail == "minimal":
        return {k: v for k, v in out.items() if v not in (None, "", [])}

    out["genres"] = [g.tag for g in (getattr(item, "genres", None) or [])]
    out["rating"] = getattr(item, "rating", None)
    out["watched"] = bool(getattr(item, "viewCount", 0) or 0)
    duration = getattr(item, "duration", None)
    if duration:
        out["minutes"] = int(duration // 60000)
    media = getattr(item, "media", None) or []
    if media:
        out["resolution"] = getattr(media[0], "videoResolution", None)
    if kind == "show":
        out["episodes"] = getattr(item, "leafCount", None)
        out["episodes_watched"] = getattr(item, "viewedLeafCount", None)
        out["seasons"] = getattr(item, "childCount", None)

    if detail == "full":
        out["content_rating"] = getattr(item, "contentRating", None)
        out["audience_rating"] = getattr(item, "audienceRating", None)
        out["studio"] = getattr(item, "studio", None)
        out["directors"] = [d.tag for d in (getattr(item, "directors", None) or [])][:3]
        out["cast"] = [r.tag for r in (getattr(item, "roles", None) or [])][:6]
        out["library"] = getattr(item, "librarySectionTitle", None)
        added = getattr(item, "addedAt", None)
        if added:
            out["added"] = str(added)[:10]
        summary = getattr(item, "summary", None)
        if summary:
            out["summary"] = summary[:300]
        if media and getattr(media[0], "parts", None):
            size = getattr(media[0].parts[0], "size", None)
            if size:
                out["gb"] = round(size / 1e9, 2)

    return {k: v for k, v in out.items() if v not in (None, "", [])}


def clean_detail(detail):
    key = text(detail, "compact").strip().lower()
    if key not in DETAIL_LEVELS:
        raise ToolError(f"Unknown detail {detail!r}.", valid_detail=list(DETAIL_LEVELS))
    return key


# ---------------------------------------------------------------------------
# Title matching - for answering "do I have this?" about a list of titles
# without one search per title.
# ---------------------------------------------------------------------------

_ROMAN = {
    " i": " 1", " ii": " 2", " iii": " 3", " iv": " 4", " v": " 5",
    " vi": " 6", " vii": " 7", " viii": " 8", " ix": " 9", " x": " 10",
}


def normalize_title(title):
    """Fold a title down to something two spellings of it can agree on.

    Accents, articles, punctuation, a trailing "(1994)" and roman numerals all
    differ between how a person names a film and how the library stores it, and
    every one of those differences would otherwise read as "you don't have it".
    """
    text = unicodedata.normalize("NFKD", str(title or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\(\s*\d{4}\s*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    for roman, digit in _ROMAN.items():
        if text.endswith(roman):
            text = text[: -len(roman)] + digit
            break
    return re.sub(r"\s+", " ", text).strip()


def parse_title_list(titles):
    """Accept a JSON array, newline-separated text, or a comma-separated line.

    Newlines win over commas when both are present, because a title can contain
    a comma and a list one-per-line is what an agent naturally produces.
    """
    if isinstance(titles, (list, tuple)):
        raw = list(titles)
    else:
        text = str(titles or "").strip()
        if not text:
            raw = []
        elif text.startswith("["):
            try:
                raw = json.loads(text)
            except ValueError:
                raise ToolError(
                    "titles looked like a JSON array but did not parse. Pass "
                    "one title per line instead."
                )
        elif "\n" in text:
            raw = text.split("\n")
        else:
            raw = text.split(",")
    out = []
    for entry in raw:
        entry = str(entry).strip().strip("-*• ").strip()
        if entry:
            out.append(entry)
    return out


def _trailing_number(norm):
    match = re.search(r"(\d+)$", norm)
    return match.group(1) if match else None


def same_entry(a, b):
    """Could these two normalized titles be the same work?

    Fuzzy matching is at its worst exactly where franchise gap analysis lives:
    'rocky 2' and 'rocky 4' differ by one character and score above any useful
    cutoff, but they are different films and calling one the other defeats the
    point of asking. A differing trailing number is disqualifying.
    """
    na, nb = _trailing_number(a), _trailing_number(b)
    return na == nb


def split_title_year(title):
    """'Alien (1979)' -> ('Alien', 1979)."""
    match = re.search(r"\(\s*(1[89]\d{2}|20\d{2})\s*\)\s*$", title.strip())
    if match:
        return title[: match.start()].strip(), int(match.group(1))
    return title.strip(), None


def episode_gaps(episodes):
    """Missing episode and season numbers, from the episodes that are present.

    Pure arithmetic over (show, season, episode) triples so it can be tested
    without a server. Only interior holes count: a season that stops at episode
    8 is a season that has aired 8 episodes as far as this can tell, and
    guessing otherwise would report every currently-airing show as broken.
    Season 0 is skipped because specials are numbered arbitrarily.
    """
    by_show = defaultdict(lambda: defaultdict(set))
    for ep in episodes:
        show = getattr(ep, "grandparentTitle", None)
        season = getattr(ep, "parentIndex", None)
        number = getattr(ep, "index", None)
        if show and season is not None and number is not None:
            by_show[show][season].add(number)

    findings = []
    for show, seasons in sorted(by_show.items()):
        for season in sorted(seasons):
            if season == 0:
                continue
            have = seasons[season]
            holes = sorted(set(range(1, max(have) + 1)) - have)
            if holes:
                findings.append({
                    "show": show,
                    "season": season,
                    "missing_episodes": holes[:20],
                    "missing_count": len(holes),
                    "have": len(have),
                    "highest_present": max(have),
                })
        numbered = sorted(x for x in seasons if x > 0)
        if numbered:
            absent = sorted(set(range(1, max(numbered) + 1)) - set(numbered))
            if absent:
                findings.append({
                    "show": show,
                    "missing_seasons": absent,
                    "seasons_present": numbered,
                })
    return findings


# ---------------------------------------------------------------------------
# Player discovery
#
# Three sources disagree about what a "player" is, and using only the first one
# is why an idle device looks like it does not exist:
#
#   /clients            devices that registered Companion with THIS server.
#                       Empty for most streaming sticks even mid-playback.
#   plex.tv/devices     everything registered to the account. Survives idle and
#                       carries the LAN address, so this is the useful list.
#   /status/sessions    what is streaming now. Says nothing about control.
#
# Registered is not the same as reachable: Roku only listens on :8324 while the
# Plex app is open. And a device whose `provides` omits "player" (Amazon Fire
# TV) can never be a target no matter what it is doing - verified against a live
# Fire TV session that returned 404 for even a read-only timeline poll.
# ---------------------------------------------------------------------------

DEVICE_CACHE_TTL = 30
PROBE_TIMEOUT = 2.5

_devices_cache = {"at": 0.0, "devices": None}


def _probe(url, timeout=PROBE_TIMEOUT):
    """Is this device's Companion listener up right now?"""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/resources", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # answered, even if it refused the path
    except Exception:
        return False


def account_devices():
    """Players known to plex.tv, with a live reachability probe on each.

    Cached briefly: plex.tv is a remote round trip and the agent tends to call
    list_players immediately before play.
    """
    now = time.time()
    if _devices_cache["devices"] is not None and now - _devices_cache["at"] < DEVICE_CACHE_TTL:
        return _devices_cache["devices"]

    devices = []
    try:
        from plexapi.myplex import MyPlexAccount

        for d in MyPlexAccount(token=PLEX_TOKEN).devices():
            provides = [x for x in (d.provides or "").split(",") if x]
            if "server" in provides:
                continue
            devices.append({
                "name": d.name,
                "product": d.product,
                "platform": d.platform,
                "machine_identifier": d.clientIdentifier,
                "provides": provides,
                "connections": list(d.connections or []),
                "last_seen": str(d.lastSeenAt) if d.lastSeenAt else None,
                "advertises_player": "player" in provides,
            })
    except Exception as exc:
        # A server-only token cannot read plex.tv. Degrade to /clients instead
        # of failing the call.
        log(f"plex.tv device lookup failed ({type(exc).__name__}: {exc})")
        _devices_cache.update(at=now, devices=[])
        return []

    targets = [d for d in devices if d["advertises_player"] and d["connections"]]
    if targets:
        with ThreadPoolExecutor(max_workers=8) as pool:
            reachable = list(pool.map(lambda d: _probe(d["connections"][0]), targets))
        for d, ok in zip(targets, reachable):
            d["reachable"] = ok
    for d in devices:
        d.setdefault("reachable", False)

    _devices_cache.update(at=now, devices=devices)
    return devices


def _ecp(entry):
    """The device's Roku ECP base URL, or None if it is not a Roku."""
    if "roku" not in (entry.get("platform") or entry.get("product") or "").lower():
        return None
    for url in entry.get("connections") or []:
        host = url.split("//", 1)[-1].split(":", 1)[0]
        if host:
            return f"http://{host}:{ROKU_ECP_PORT}"
    return None


def wake_plex(entry, wait_seconds=20):
    """Launch the Plex channel on a Roku and wait for Companion to come up.

    Returns (ok, detail). A Roku answers ECP from the home screen but only
    listens on :8324 once Plex is open, so this is the difference between "the
    app is closed, go turn it on" and just playing the thing.
    """
    base = _ecp(entry)
    if not base:
        return False, "not a Roku - cannot be woken remotely"

    try:
        req = urllib.request.Request(
            f"{base}/launch/{ROKU_PLEX_CHANNEL_ID}", data=b"", method="POST"
        )
        urllib.request.urlopen(req, timeout=5).close()
    except urllib.error.HTTPError as exc:
        return False, f"Roku refused the launch (HTTP {exc.code})"
    except Exception as exc:
        # No ECP response at all means the device is powered off, not busy.
        return False, (
            f"no response from the Roku at {base} ({type(exc).__name__}) - "
            "the device is powered off"
        )

    target = (entry.get("connections") or [None])[0]
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if target and _probe(target, timeout=2):
            _devices_cache["devices"] = None  # reachability changed
            return True, "Plex launched and the player is responding"
        time.sleep(1.5)
    return False, (
        f"Plex was launched but the player did not start responding within "
        f"{wait_seconds}s"
    )


def discover_players():
    """Every player the agent could plausibly mean, each with why it can or
    cannot be driven right now. Ordered: usable first."""
    p = plex()

    live = {}
    for c in p.clients():
        mid = getattr(c, "machineIdentifier", None)
        live[mid] = c

    streaming = {}
    for session in p.sessions():
        for pl in getattr(session, "players", []) or []:
            streaming[getattr(pl, "machineIdentifier", None)] = getattr(pl, "state", None)

    out = []
    seen = set()
    for d in account_devices():
        mid = d["machine_identifier"]
        seen.add(mid)
        entry = dict(d)
        entry["streaming_now"] = streaming.get(mid)
        if mid in live:
            entry.update(controllable=True, route="server",
                         status="ready (registered with the Plex server)")
        elif not d["advertises_player"]:
            entry.update(controllable=False, route=None, status=(
                "cannot be controlled - this app never advertises itself as a "
                "player. No API call will work. Reporting this is the answer."))
        elif d["reachable"]:
            entry.update(controllable=True, route="direct",
                         status="ready (reachable on its LAN address)")
        else:
            entry.update(controllable=False, route=None, status=(
                "registered but not listening - the Plex app is closed on this "
                "device. Open Plex on it, then retry."))
        # Browser tabs and controller-only apps (Home Assistant, the web UI)
        # register forever and are never playback targets. Listing them buries
        # the real players. Keep one only if it is streaming, so the "why can I
        # not control the thing that is obviously playing" case stays visible.
        entry["relevant"] = bool(d["advertises_player"] or entry["streaming_now"])
        out.append(entry)

    # Anything in /clients that plex.tv did not mention.
    for mid, c in live.items():
        if mid in seen:
            continue
        out.append({
            "name": c.title, "product": getattr(c, "product", None),
            "platform": getattr(c, "platform", None), "machine_identifier": mid,
            "provides": ["player"], "connections": [], "last_seen": None,
            "advertises_player": True, "reachable": True, "controllable": True,
            "route": "server", "streaming_now": streaming.get(mid),
            "status": "ready (registered with the Plex server)", "relevant": True,
        })

    out.sort(key=lambda d: (not d["controllable"], (d["name"] or "").lower()))
    return out


def build_client(entry):
    """A command-ready PlexClient for a discovered player.

    `route` decides how commands travel. Note PlexClient(identifier=...) does
    NOT set machineIdentifier - that argument only feeds connect()'s lookup,
    which we skip because it dials the player directly and that is the step
    that fails. sendCommand reads machineIdentifier for the target header, so
    set it directly.
    """
    from plexapi.client import PlexClient

    p = plex()
    if entry["route"] == "direct":
        baseurl = entry["connections"][0]
        proxy = False
    else:
        baseurl = p._baseurl
        proxy = True

    client = PlexClient(server=p, baseurl=baseurl, token=p._token, connect=False)
    client.machineIdentifier = entry["machine_identifier"]
    client.title = entry["name"]
    client.product = entry.get("product") or ""
    client.protocolCapabilities = [
        "timeline", "playback", "navigation", "mirror", "playqueues",
    ]
    client.proxyThroughServer(proxy, p)
    return client


# ---------------------------------------------------------------------------
# Resolution helpers - the parts that usually cause the "why did it not play"
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """An error with a message meant to be shown verbatim to the agent."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def resolve_player(name):
    """Find a player by exact, prefix, then substring match (case-insensitive).

    Matches against every known player, not just the controllable ones, so that
    naming a device which exists but cannot be driven returns the specific
    reason instead of "no player matches" - the agent should report that reason,
    not go looking for a workaround.
    """
    players = discover_players()
    usable = [d for d in players if d["controllable"]]
    # Error payloads list only plausible targets. Naming all 18 registered
    # browser tabs teaches the agent nothing and invites it to try them.
    notable = [d for d in players if d.get("relevant")]

    if not players:
        raise ToolError(
            "Plex knows of no players at all on this account. Either PLEX_TOKEN "
            "is a server-only token that cannot read plex.tv, or no Plex client "
            "app has ever signed in."
        )

    if not name:
        if len(usable) == 1:
            return build_client(usable[0])
        if not usable:
            raise ToolError(
                "No player is controllable right now.",
                players=[{"name": d["name"], "status": d["status"]} for d in notable],
            )
        raise ToolError(
            "No player specified and more than one is available.",
            available_players=[d["name"] for d in usable],
        )

    want = name.strip().lower()
    # A room name ("theater") is what gets said out loud; translate before
    # matching so aliases work with every downstream match rule.
    want = PLEX_ALIASES.get(want, want).strip().lower()

    def haystack(d):
        # Some clients report a useless name - the Fire TV registers as
        # literally "unknown" - so product and platform have to be searchable
        # or there is no way to refer to the device at all.
        return " ".join(
            filter(None, [d.get("name"), d.get("product"), d.get("platform")])
        ).lower()

    for match in (
        lambda d: (d["name"] or "").lower() == want,
        lambda d: (d["name"] or "").lower().startswith(want),
        lambda d: want in (d["name"] or "").lower(),
        lambda d: want in haystack(d),
    ):
        matches = [d for d in players if match(d)]
        if matches:
            break

    if not matches:
        raise ToolError(
            f"No player matches {name!r}.",
            available_players=[d["name"] for d in usable],
            all_known_players=[
                {"name": d["name"], "status": d["status"]} for d in notable
            ],
        )
    if len(matches) > 1:
        usable_matches = [d for d in matches if d["controllable"]]
        if len(usable_matches) != 1:
            raise ToolError(
                f"{name!r} is ambiguous.",
                candidates=[d["name"] for d in matches],
            )
        matches = usable_matches

    chosen = matches[0]
    if not chosen["controllable"]:
        # A Roku with its app closed is a solvable problem, not a refusal.
        if chosen["advertises_player"] and _ecp(chosen):
            log(f"{chosen['name']!r} is asleep; launching Plex on it")
            woke, detail = wake_plex(chosen)
            if woke:
                refreshed = [
                    d for d in discover_players()
                    if d["machine_identifier"] == chosen["machine_identifier"]
                ]
                if refreshed and refreshed[0]["controllable"]:
                    return build_client(refreshed[0])
            raise ToolError(
                f"{chosen['name']!r} could not be woken: {detail}",
                player=chosen["name"],
                controllable_players=[d["name"] for d in usable],
            )
        raise ToolError(
            f"{chosen['name']!r} cannot be controlled: {chosen['status']}",
            player=chosen["name"],
            product=chosen.get("product"),
            streaming_now=chosen.get("streaming_now"),
            controllable_players=[d["name"] for d in usable],
        )
    return build_client(chosen)


def find_media(query, media_type=None, limit=10):
    """Fuzzy hub search first, exact title search per section as a fallback.

    Hub search tolerates the misspellings that come out of voice transcription;
    the title fallback catches items hub search ranks poorly.
    """
    p = plex()
    results, seen = [], set()

    def add(item):
        key = str(getattr(item, "ratingKey", ""))
        if key and key not in seen and getattr(item, "type", None) in (
            "movie", "show", "episode", "artist", "album", "track", "season",
        ):
            seen.add(key)
            results.append(item)

    try:
        for item in p.search(query, mediatype=media_type, limit=limit):
            add(item)
    except Exception as exc:  # hub search is fussy about mediatype values
        log(f"hub search failed ({exc}); falling back to per-section search")

    if len(results) < limit:
        wanted_sections = {"movie": "movie", "show": "show", "episode": "show"}
        for section in p.library.sections():
            if media_type and section.type != wanted_sections.get(
                media_type, section.type
            ):
                continue
            try:
                for item in section.search(title=query, limit=limit):
                    add(item)
            except Exception as exc:
                log(f"section {section.title!r} search failed: {exc}")

    return results[:limit]


def get_by_rating_key(rating_key):
    return plex().fetchItem(int(rating_key))


def stop_session(player_name=None):
    """Terminate a stream from the server side, bypassing Companion entirely.

    Returns None if no matching session is streaming, so the caller can fall
    back to a normal client stop.
    """
    sessions = plex().sessions()
    if not sessions:
        return None

    want = (player_name or "").strip().lower()
    want = PLEX_ALIASES.get(want, want).strip().lower()

    def player_of(session):
        players = getattr(session, "players", []) or []
        return players[0] if players else None

    if want:
        chosen = None
        for session in sessions:
            pl = player_of(session)
            hay = " ".join(
                filter(None, [getattr(pl, "title", ""), getattr(pl, "product", "")])
            ).lower()
            if pl and want in hay:
                chosen = session
                break
        if chosen is None:
            return None
    elif len(sessions) == 1:
        chosen = sessions[0]
    else:
        raise ToolError(
            "More than one thing is playing; say which player to stop.",
            playing=[
                {"player": getattr(player_of(x), "title", "?"), "title": x.title}
                for x in sessions
            ],
        )

    pl = player_of(chosen)
    chosen.stop(reason="Stopped by Hermes")
    return {
        "ok": True,
        "action": "stop",
        "player": getattr(pl, "title", "?"),
        "stopped": chosen.title,
        "method": "server-side session terminate",
    }


def confirm_playback(machine_identifier, item, wait_seconds=8):
    """Did the thing we asked for actually start?

    A client accepting playMedia is not the same as a client playing something -
    they diverge often enough that reporting the first as the second is how an
    agent ends up telling someone a movie is on when the screen is black.
    """
    want = str(getattr(item, "ratingKey", ""))
    deadline = time.time() + wait_seconds
    last = "no session appeared"
    while time.time() < deadline:
        try:
            for session in plex().sessions():
                for pl in getattr(session, "players", []) or []:
                    if getattr(pl, "machineIdentifier", None) != machine_identifier:
                        continue
                    if str(getattr(session, "ratingKey", "")) == want:
                        return {
                            "confirmed": True,
                            "detail": f"{getattr(pl, 'state', 'playing')} at "
                                      f"{ms_to_clock(getattr(session, 'viewOffset', 0))}",
                        }
                    last = f"player is on a different title ({session.title!r})"
        except Exception as exc:
            last = f"could not read sessions ({type(exc).__name__})"
        time.sleep(1.5)
    return {"confirmed": False, "detail": last}


def next_unwatched_episode(show):
    """onDeck first (respects partial watches), else first unwatched, else pilot."""
    try:
        deck = show.onDeck()
        if deck:
            return deck
    except Exception:
        pass
    for ep in show.episodes():
        if not ep.isPlayed:
            return ep
    eps = show.episodes()
    return eps[0] if eps else None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool("Check the Plex connection and summarize the server. Call this first when anything is not working.")
def plex_status():
    p = plex()
    sections = [
        {"title": sec.title, "type": sec.type, "items": sec.totalSize}
        for sec in p.library.sections()
    ]
    players = discover_players()
    return {
        "ok": True,
        "server": p.friendlyName,
        "version": p.version,
        "url": PLEX_URL,
        "proxy_through_server": PLEX_PROXY,
        "libraries": sections,
        "players": [d["name"] for d in players if d["controllable"]],
        "players_unavailable": [
            {"name": d["name"], "reason": d["status"]}
            for d in players if not d["controllable"] and d.get("relevant")
        ],
        "active_sessions": len(p.sessions()),
    }


@tool(
    "List every known Plex player and whether it can be controlled right now. "
    "Playback is NOT required for a device to appear here. Use the 'name' value "
    "verbatim as the 'player' argument elsewhere.",
    {
        "only_controllable": b("Return only players that can be driven right now."),
        "include_all": b(
            "Also list browser tabs and controller-only apps that are never "
            "playback targets. Rarely useful."
        ),
    },
)
def list_players(only_controllable=False, include_all=False):
    players = [
        d for d in discover_players() if include_all or d.get("relevant")
    ]
    usable = [d for d in players if d["controllable"]]
    shown = usable if only_controllable else players
    blocked = [d for d in players if not d["controllable"]]
    return {
        "ok": True,
        "players": shown,
        "count": len(shown),
        "controllable": [d["name"] for d in usable],
        "unavailable": [
            {"name": d["name"], "reason": d["status"]} for d in blocked
        ],
        "note": (
            "A player with controllable=false cannot be driven. When the reason "
            "says the app never advertises itself as a player, that is final - "
            "report it and stop. No argument variation or alternate API fixes it."
        ),
    }


@tool(
    "Map of what is actually in the libraries: sections, sizes, and the exact "
    "genre/decade/rating vocabulary each one accepts. Call this before 'discover' "
    "so filters use real values instead of guesses.",
    {"library": s("Limit to one library by name. Default: all of them.")},
)
def library_overview(library=None):
    p = plex()
    out = []
    for section in p.library.sections():
        if library and library.strip().lower() not in section.title.lower():
            continue
        entry = {
            "library": section.title,
            "type": section.type,
            "total_items": section.totalSize,
        }
        names = {
            "genre": "genres", "decade": "decades", "contentRating":
            "content_ratings", "resolution": "resolutions",
            "country": "countries",
        }
        for field, label in names.items():
            try:
                entry[label] = [c.title for c in section.listFilterChoices(field)]
            except Exception:
                pass
        # 257 studios is a wall of text nobody reads; the count is the useful
        # part and 'discover' takes a studio name whether or not it is listed.
        try:
            entry["studio_count"] = len(section.listFilterChoices("studio"))
        except Exception:
            pass
        out.append(entry)
    if not out:
        raise ToolError(
            f"No library matches {library!r}.",
            available=[x.title for x in p.library.sections()],
        )
    return {
        "ok": True,
        "libraries": out,
        "note": (
            "Use these exact genre strings with 'discover'. Genres are "
            "combinable - a fantasy epic is usually Fantasy plus Adventure."
        ),
    }


@tool(
    "Find media by genre, decade, rating and watched state - the tool for open "
    "requests like 'a fantasy epic' or 'something short and funny I have not "
    "seen'. Returns full metadata for reasoning. Call library_overview first for "
    "valid genre names.",
    {
        "genre": s("Genre, or several separated by commas (all must match)."),
        "library": s("Library name. Defaults to Movies if present."),
        "decade": s("Decade like '1990s', or a plain year like '1994'."),
        "min_rating": i("Minimum critic rating, 0-10."),
        "unwatched_only": b("Only things not yet watched."),
        "actor": s("Filter by actor name."),
        "director": s("Filter by director name."),
        "sort": s("rating, random, recent, title, or year. Default rating.", "rating"),
        "match_all": b(
            "With several genres, require all of them (a fantasy epic is both "
            "Fantasy and Adventure). Set false to match any.", True),
        "resolution": s("Filter by resolution: 4k, 1080, 720, sd."),
        "studio": s("Filter by studio name."),
        "country": s("Filter by country."),
        "content_rating": s("Filter by content rating, e.g. 'R' or 'PG-13'."),
        "detail": s(
            "minimal (title/year), compact (adds genres, rating, watched, "
            "resolution), or full (adds cast, summary, studio, file size). "
            "Default compact.", "compact"),
        "limit": i("How many to return. Default 8. There is no small cap - ask "
                   "for 500 if you want 500.", 8),
        "offset": i("Skip this many results, for paging through a big set.", 0),
    },
)
def discover(genre=None, library=None, decade=None, min_rating=None,
             unwatched_only=False, actor=None, director=None,
             sort="rating", match_all=True, resolution=None, studio=None,
             country=None, content_rating=None, detail="compact",
             limit=8, offset=0):
    p = plex()
    detail = clean_detail(detail)
    sections = p.library.sections()
    if library:
        want = library.strip().lower()
        sections = [x for x in sections if want in x.title.lower()]
        if not sections:
            raise ToolError(
                f"No library matches {library!r}.",
                available=[x.title for x in p.library.sections()],
            )
    else:
        movies = [x for x in sections if x.type == "movie"]
        sections = movies or sections
    section = sections[0]

    sorts = {
        "rating": "rating:desc",
        "random": "random",
        "recent": "addedAt:desc",
        "newest": "addedAt:desc",
        "title": "titleSort:asc",
        "year": "year:desc",
    }
    sort_key = sorts.get(text(sort, "rating").strip().lower())
    if sort_key is None:
        raise ToolError(f"Unknown sort {sort!r}.", valid_sorts=sorted(sorts))

    filters = {}
    genres = [g.strip() for g in text(genre).split(",") if g.strip()]
    if genres:
        # Plex joins a list with "," as OR (genre=6,5). Appending "&" to the
        # field emits repeated params (genre=6&genre=5), which is AND. "A
        # fantasy epic" means both tags, so AND is the useful default.
        filters["genre&" if (match_all and len(genres) > 1) else "genre"] = genres
    if decade:
        d = text(decade).strip()
        # library_overview reports decades as "1990s" because that is how Plex
        # labels the filter choice, but the filter itself only accepts the bare
        # integer - passing back the value we advertised was a hard error.
        if d.lower().endswith("s"):
            filters["decade"] = d[:-1]
        else:
            filters["year"] = d
    if min_rating is not None:
        filters["rating>>"] = float(min_rating)
    if unwatched_only:
        filters["unwatched"] = True
    if actor:
        filters["actor"] = actor
    if director:
        filters["director"] = director
    if resolution:
        filters["resolution"] = text(resolution).strip().lower().rstrip("p")
    if studio:
        filters["studio"] = studio
    if country:
        filters["country"] = country
    if content_rating:
        filters["contentRating"] = content_rating

    limit = max(1, int(limit or 8))
    offset = max(0, int(offset or 0))
    # "full" prints cast, summary and file size per row; a few hundred of those
    # is a context window, not an answer. Cap it and say so rather than
    # silently returning something that cannot be read.
    capped = None
    if detail == "full" and limit > 50:
        capped, limit = limit, 50
    try:
        results = section.search(
            filters=filters or None, sort=sort_key,
            container_start=offset, maxresults=limit,
        )
    except Exception as exc:
        raise ToolError(
            f"Plex rejected those filters: {type(exc).__name__}: {exc}",
            filters_used=filters,
            hint="Call library_overview for the exact genre and decade values.",
        )

    if not results:
        raise ToolError(
            "Nothing in the library matches those filters."
            + (f" (offset {offset} may be past the end)" if offset else ""),
            filters_used=filters,
            library=section.title,
            hint="Drop the most specific filter and try again, or report that "
                 "the library has nothing matching rather than inventing titles.",
        )

    degraded = 0
    if detail != "minimal":
        results, degraded = enrich_items(results)

    out = {
        "ok": True,
        "library": section.title,
        "filters_used": filters,
        "sort": sort_key,
        "detail": detail,
        "offset": offset,
        "count": len(results),
        "results": [project_item(x, detail) for x in results],
    }
    if len(results) == limit:
        out["next_offset"] = offset + limit
        out["note"] = (
            f"Returned a full page of {limit}. There may be more - call again "
            f"with offset={offset + limit}, or raise limit."
        )
    if capped:
        out["limit_capped"] = (
            f"Asked for {capped} at detail=full; returned 50. Use "
            "detail=compact or detail=minimal for larger sets."
        )
    if degraded:
        out["metadata_incomplete"] = (
            f"Up to {degraded} items fell back to truncated listing metadata; "
            "their genre lists may be short."
        )
    return out


@tool(
    "The complete contents of a library in one call - every title, not a page "
    "of them. This is the tool for whole-library questions: what am I missing, "
    "what is the shape of my collection, do I have enough of X. Use "
    "detail=minimal for a 500-title inventory at a few thousand tokens; only "
    "raise detail on a narrowed set. Do NOT loop 'discover' to enumerate a "
    "library - that is what this replaces.",
    {
        "library": s("Library name. Defaults to Movies if present."),
        "media_type": s(
            "For TV libraries: 'show' for series (default), 'episode' for every "
            "episode individually, 'season' for seasons."),
        "detail": s(
            "minimal (title/year - use this for whole libraries), compact "
            "(adds genres, rating, watched, resolution), full (adds cast, "
            "summary, file size - only for small sets). Default minimal.",
            "minimal"),
        "unwatched_only": b("Only items not yet watched."),
        "sort": s("title, year, rating, recent, or random. Default title.", "title"),
        "limit": i("Cap the number returned. Default: everything."),
        "offset": i("Skip this many, for chunking a very large library.", 0),
    },
)
def library_export(library=None, media_type=None, detail="minimal",
                   unwatched_only=False, sort="title", limit=None, offset=0):
    detail = clean_detail(detail)
    sections = resolve_sections(library)
    if not library:
        movies = [x for x in sections if x.type == "movie"]
        sections = movies or sections[:1]
    section = sections[0]

    libtype = text(media_type).strip().lower() or None
    if libtype and section.type == "movie" and libtype != "movie":
        raise ToolError(
            f"{section.title!r} is a movie library; media_type={media_type!r} "
            "does not apply there.",
            hint="Drop media_type, or name a TV library.",
        )

    items, degraded = section_items(
        section, libtype=libtype, enriched=(detail != "minimal")
    )

    if unwatched_only:
        if libtype == "show" or (section.type == "show" and not libtype):
            items = [x for x in items
                     if (getattr(x, "viewedLeafCount", 0) or 0) == 0]
        else:
            items = [x for x in items if not (getattr(x, "viewCount", 0) or 0)]

    sorters = {
        "title": lambda x: (getattr(x, "titleSort", None)
                            or getattr(x, "title", "") or "").lower(),
        "year": lambda x: -(getattr(x, "year", 0) or 0),
        "rating": lambda x: -(getattr(x, "rating", 0) or 0),
        "recent": lambda x: -(getattr(x, "addedAt", None).timestamp()
                              if getattr(x, "addedAt", None) else 0),
    }
    key = text(sort, "title").strip().lower()
    if key == "random":
        items = list(items)
        random.shuffle(items)
    elif key in sorters:
        items = sorted(items, key=sorters[key])
    else:
        raise ToolError(f"Unknown sort {sort!r}.",
                        valid_sorts=sorted(list(sorters) + ["random"]))

    total = len(items)
    offset = max(0, int(offset or 0))
    window = items[offset:]
    capped = None
    if detail == "full" and (limit is None or int(limit) > 50):
        capped, limit = limit or total, 50
    if limit is not None:
        window = window[: max(1, int(limit))]

    out = {
        "ok": True,
        "library": section.title,
        "media_type": libtype or section.type,
        "detail": detail,
        "total_matching": total,
        "returned": len(window),
        "offset": offset,
        "items": [project_item(x, detail) for x in window],
    }
    if offset + len(window) < total:
        out["next_offset"] = offset + len(window)
        out["note"] = (
            f"{total - offset - len(window)} more items. Call again with "
            f"offset={offset + len(window)}."
        )
    else:
        out["complete"] = True
        out["note"] = (
            "This is the entire matching set. Every title in this library is "
            "listed above - anything not here is not on the server."
        )
    if capped:
        out["limit_capped"] = (
            f"detail=full is capped at 50 items (asked for {capped}). Use "
            "detail=minimal or compact to list the whole library."
        )
    if degraded:
        out["metadata_incomplete"] = (
            f"Up to {degraded} items fell back to truncated listing metadata."
        )
    return out


@tool(
    "The shape of a library in numbers: counts by decade, genre, resolution, "
    "content rating and watched state, plus year span and disk usage. Call "
    "this before hunting for gaps - it shows where the collection is thin "
    "without listing a single title.",
    {"library": s("Library name. Default: every library.")},
)
def library_stats(library=None):
    sections = resolve_sections(library)
    report = []
    for section in sections:
        items, _ = section_items(section, enriched=True)
        if not items:
            report.append({"library": section.title, "total": 0})
            continue

        years = [x.year for x in items if getattr(x, "year", None)]
        genres = Counter(
            g.tag for x in items for g in (getattr(x, "genres", None) or [])
        )
        decades = Counter(
            f"{(y // 10) * 10}s" for y in years
        )
        resolutions = Counter(
            (x.media[0].videoResolution or "unknown")
            for x in items if getattr(x, "media", None)
        )
        ratings = Counter(
            getattr(x, "contentRating", None) or "unrated" for x in items
        )
        size = sum(
            (part.size or 0)
            for x in items for m in (getattr(x, "media", None) or [])
            for part in (getattr(m, "parts", None) or [])
        )

        entry = {
            "library": section.title,
            "type": section.type,
            "total": len(items),
            "year_span": (
                {"oldest": min(years), "newest": max(years)} if years else None
            ),
            "by_decade": dict(sorted(decades.items())),
            "by_genre": dict(genres.most_common(30)),
            "by_resolution": dict(resolutions.most_common()),
            "by_content_rating": dict(ratings.most_common()),
            "missing_year": sum(1 for x in items if not getattr(x, "year", None)),
            "missing_genres": sum(
                1 for x in items if not (getattr(x, "genres", None) or [])
            ),
        }
        if size:
            entry["disk_gb"] = round(size / 1e9, 1)

        if section.type == "show":
            entry["episodes"] = sum(
                (getattr(x, "leafCount", 0) or 0) for x in items
            )
            entry["episodes_watched"] = sum(
                (getattr(x, "viewedLeafCount", 0) or 0) for x in items
            )
            entry["shows_untouched"] = sum(
                1 for x in items if not (getattr(x, "viewedLeafCount", 0) or 0)
            )
        else:
            watched = sum(1 for x in items if getattr(x, "viewCount", 0) or 0)
            entry["watched"] = watched
            entry["unwatched"] = len(items) - watched
        report.append(entry)

    return {
        "ok": True,
        "libraries": report,
        "note": (
            "An empty or thin decade bucket is the clearest gap signal here - "
            "compare by_decade against what a collection of this size would "
            "normally cover."
        ),
    }


@tool(
    "Check a whole list of titles against the library at once and report which "
    "are present, which are missing, and which are too close to call. This is "
    "the tool for gap analysis: propose the classics or the franchise entries "
    "you think should be there, pass them all in one call, and get back "
    "exactly what is absent. Never guess whether the server has something - "
    "ask here.",
    {
        "titles": s(
            "The titles to check - one per line, or a JSON array. A year in "
            "parentheses ('Alien (1979)') is used to disambiguate remakes."),
        "library": s("Library to check against. Default: every library."),
        "media_type": s("Restrict to 'movie' or 'show'."),
    },
    ["titles"],
)
def check_titles(titles, library=None, media_type=None):
    wanted = parse_title_list(titles)
    if not wanted:
        raise ToolError("No titles given. Pass one title per line.")

    sections = resolve_sections(library, media_type)
    if not sections:
        raise ToolError(
            f"No library matches media_type={media_type!r}.",
            available=[x.title for x in plex().library.sections()],
        )

    index = defaultdict(list)
    for section in sections:
        items, _ = section_items(section, enriched=False)
        for item in items:
            index[normalize_title(getattr(item, "title", ""))].append(item)
            # Plex stores the localized title as `title` and often keeps the
            # original under originalTitle. A person asking for "Spirited Away"
            # and a library holding "Sen to Chihiro" are the same film.
            original = getattr(item, "originalTitle", None)
            if original:
                index[normalize_title(original)].append(item)

    keys = list(index)
    present, missing, uncertain = [], [], []

    for raw in wanted:
        bare, year = split_title_year(raw)
        norm = normalize_title(bare)
        hits = index.get(norm) or []

        if hits:
            if year:
                exact = [x for x in hits
                         if getattr(x, "year", None)
                         and abs(x.year - year) <= 1]
                if not exact:
                    uncertain.append({
                        "asked": raw,
                        "found": f"{hits[0].title} ({getattr(hits[0], 'year', '?')})",
                        "why": "title matches but the year does not - likely a "
                               "different cut or a remake",
                        "rating_key": str(hits[0].ratingKey),
                    })
                    continue
                hits = exact
            present.append({
                "asked": raw,
                "title": hits[0].title,
                "year": getattr(hits[0], "year", None),
                "rating_key": str(hits[0].ratingKey),
                "watched": bool(getattr(hits[0], "viewCount", 0) or 0),
            })
            continue

        # A near miss is reported as its own outcome, never folded into
        # "present". Calling a fuzzy match a hit is how an agent ends up
        # telling someone they own a film they do not.
        close = [x for x in difflib.get_close_matches(norm, keys, n=4, cutoff=0.85)
                 if same_entry(norm, x)]
        if close:
            candidate = index[close[0]][0]
            uncertain.append({
                "asked": raw,
                "found": f"{candidate.title} ({getattr(candidate, 'year', '?')})",
                "why": "close but not an exact title match - confirm before "
                       "treating it as present",
                "rating_key": str(candidate.ratingKey),
            })
        else:
            missing.append(raw)

    return {
        "ok": True,
        "checked": len(wanted),
        "libraries_searched": [x.title for x in sections],
        "present_count": len(present),
        "missing_count": len(missing),
        "missing": missing,
        "present": present,
        "uncertain": uncertain,
        "note": (
            "'missing' is authoritative - those titles are not on the server. "
            "'uncertain' needs a human or a follow-up search before you call it "
            "either way."
        ),
    }


@tool(
    "Find holes in the collection: TV seasons with episodes missing, movies "
    "still stuck at low resolution, and items with broken or absent metadata. "
    "All computed from what is on the server, so it is exact - no guessing "
    "about what 'should' be there.",
    {
        "kind": s(
            "episodes (missing TV episodes and seasons), quality (low-res "
            "files), metadata (items Plex could not match properly), or all. "
            "Default all.", "all"),
        "library": s("Restrict to one library."),
        "min_resolution": s(
            "For kind=quality: flag anything below this. 1080 or 720. "
            "Default 1080.", "1080"),
        "limit": i("Maximum findings per category. Default 40.", 40),
    },
)
def find_gaps(kind="all", library=None, min_resolution="1080", limit=40):
    kind = text(kind, "all").strip().lower()
    valid = ("all", "episodes", "quality", "metadata")
    if kind not in valid:
        raise ToolError(f"Unknown kind {kind!r}.", valid_kinds=list(valid))
    limit = max(1, int(limit or 40))
    sections = resolve_sections(library)
    out = {"ok": True, "kind": kind}

    if kind in ("all", "episodes"):
        findings = []
        for section in (x for x in sections if x.type == "show"):
            # One request returns every episode in the library with its show,
            # season and episode number. Gaps are then pure arithmetic - no
            # per-show walk, no external episode list needed.
            episodes, _ = section_items(section, libtype="episode", enriched=False)
            findings.extend(episode_gaps(episodes))
        out["episode_gaps"] = findings[:limit]
        out["episode_gap_count"] = len(findings)
        out["episode_gap_caveat"] = (
            "Gaps are inferred from the episode numbers present, so a show "
            "that numbers episodes absolutely rather than per-season can read "
            "as a gap. Check 'highest_present' against the real season length "
            "before acting."
        )

    if kind in ("all", "quality"):
        want = text(min_resolution, "1080").strip().lower().rstrip("p")
        ranking = {"sd": 0, "480": 0, "576": 1, "720": 2, "1080": 3, "4k": 4}
        floor = ranking.get(want)
        if floor is None:
            raise ToolError(
                f"Unknown min_resolution {min_resolution!r}.",
                valid=["720", "1080", "4k"],
            )
        low = []
        for section in (x for x in sections if x.type == "movie"):
            items, _ = section_items(section, enriched=False)
            for item in items:
                media = getattr(item, "media", None) or []
                if not media:
                    continue
                res = (media[0].videoResolution or "").lower()
                if ranking.get(res, 99) < floor:
                    low.append({
                        "title": item.title,
                        "year": getattr(item, "year", None),
                        "resolution": res or "unknown",
                        "rating_key": str(item.ratingKey),
                    })
        low.sort(key=lambda d: (d["resolution"], d["title"]))
        out["below_" + want] = low[:limit]
        out["below_" + want + "_count"] = len(low)

    if kind in ("all", "metadata"):
        broken = []
        for section in sections:
            items, _ = section_items(section, enriched=True)
            for item in items:
                reasons = []
                if not getattr(item, "year", None):
                    reasons.append("no year")
                if not (getattr(item, "genres", None) or []):
                    reasons.append("no genres")
                if not getattr(item, "summary", None):
                    reasons.append("no summary")
                # An unmatched file keeps its filename as the title, and
                # filenames carry the release-group debris real titles never do.
                title = getattr(item, "title", "") or ""
                if re.search(r"\b(1080p|720p|x264|x265|bluray|webrip|hdtv)\b",
                             title, re.I):
                    reasons.append("title looks like a filename - unmatched")
                if reasons:
                    broken.append({
                        "title": title,
                        "library": section.title,
                        "year": getattr(item, "year", None),
                        "problems": reasons,
                        "rating_key": str(item.ratingKey),
                    })
        out["metadata_problems"] = broken[:limit]
        out["metadata_problem_count"] = len(broken)
        out["metadata_hint"] = (
            "refresh_item fixes most of these; ones that stay broken need "
            "matching by hand in Plex."
        )

    return out


@tool(
    "Given something the user liked, find comparable titles in the library, "
    "ranked by how many genres they share. Use for 'something like X'.",
    {
        "title": s("A title the user already likes."),
        "unwatched_only": b("Only suggest things not yet watched.", True),
        "limit": i("How many to return. Default 6.", 6),
    },
    ["title"],
)
def similar_to(title, unwatched_only=True, limit=6):
    p = plex()
    seed_matches = find_media(title, None, 3)
    if not seed_matches:
        raise ToolError(f"Nothing in the library matches {title!r}.")
    seed = seed_matches[0]
    seed.reload()
    seed_genres = {g.tag for g in (getattr(seed, "genres", None) or [])}
    if not seed_genres:
        raise ToolError(
            f"{seed.title!r} has no genre metadata, so there is nothing to "
            "compare against. Refresh its metadata in Plex."
        )

    section = p.library.sectionByID(seed.librarySectionID)
    pool, seen = [], {str(seed.ratingKey)}
    # One query per shared genre beats pulling the whole library and is still
    # only a handful of requests.
    for tag in list(seed_genres)[:4]:
        try:
            found = section.search(
                filters={"genre": tag, **({"unwatched": True} if unwatched_only else {})},
                sort="rating:desc", maxresults=40,
            )
        except Exception:
            continue
        for item in found:
            key = str(item.ratingKey)
            if key not in seen:
                seen.add(key)
                pool.append(item)

    # The candidate pool runs to a couple of hundred items and every one of them
    # needs its untruncated genre list to be scored. Batched, that is two or
    # three requests; the per-item reload this replaced was one request each.
    pool, _ = enrich_items(pool)

    scored = []
    for item in pool:
        overlap = seed_genres & {g.tag for g in (getattr(item, "genres", None) or [])}
        if overlap:
            scored.append((len(overlap), getattr(item, "rating", 0) or 0, item, overlap))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    if not scored:
        raise ToolError(
            f"Nothing in the library shares a genre with {seed.title!r}.",
            seed_genres=sorted(seed_genres),
        )

    limit = max(1, min(int(limit or 6), 15))
    return {
        "ok": True,
        "seed": {"title": seed.title, "year": seed.year, "genres": sorted(seed_genres)},
        "unwatched_only": unwatched_only,
        "results": [
            {**describe_item(item), "shared_genres": sorted(shared),
             "rating": rating or None}
            for _, rating, item, shared in scored[:limit]
        ],
    }


@tool("List the libraries on the Plex server.")
def list_libraries():
    return {
        "ok": True,
        "libraries": [
            {"title": sec.title, "type": sec.type, "items": sec.totalSize}
            for sec in plex().library.sections()
        ],
    }


@tool(
    "Search the Plex libraries by title. Returns rating_key values that play_rating_key can use.",
    {
        "query": s("Title to search for. Fuzzy - misheard titles usually still match."),
        "media_type": s("Optional filter: movie, show, episode, artist, album, track."),
        "limit": i("Maximum results.", 10),
    },
    ["query"],
)
def search(query, media_type=None, limit=10):
    items = find_media(query, media_type, int(limit))
    return {
        "ok": True,
        "query": query,
        "count": len(items),
        "results": [describe_item(x) for x in items],
    }


@tool(
    "Search for a title and play the best match on a player. This is the main playback tool.",
    {
        "query": s("Title to find and play."),
        "player": s("Player name from list_players. Optional if only one player exists."),
        "media_type": s("Optional filter: movie, show, episode."),
        "offset_seconds": i("Start position in seconds.", 0),
    },
    ["query"],
)
def play(query, player=None, media_type=None, offset_seconds=0):
    items = find_media(query, media_type, 5)
    if not items:
        raise ToolError(f"Nothing in the Plex libraries matches {query!r}.")

    target = items[0]
    # Asking for a show means "put the show on", not "open a menu".
    if target.type == "show":
        episode = next_unwatched_episode(target)
        if episode is None:
            raise ToolError(f"{target.title!r} has no episodes to play.")
        target = episode

    client = resolve_player(player)
    client.playMedia(target, offset=int(offset_seconds) * 1000)
    started = confirm_playback(client.machineIdentifier, target)
    return {
        "ok": True,
        "action": "playing" if started["confirmed"] else "command accepted",
        "player": client.title,
        "confirmed_playing": started["confirmed"],
        "playback_state": started["detail"],
        "now_playing": describe_item(target),
        "other_matches": [describe_item(x) for x in items[1:4]],
    }


@tool(
    "Play an exact item by rating_key, from a previous search. Use when search returned several plausible matches and the right one has been chosen.",
    {
        "rating_key": s("rating_key from a search result."),
        "player": s("Player name from list_players."),
        "offset_seconds": i("Start position in seconds.", 0),
    },
    ["rating_key"],
)
def play_rating_key(rating_key, player=None, offset_seconds=0):
    item = get_by_rating_key(rating_key)
    client = resolve_player(player)
    client.playMedia(item, offset=int(offset_seconds) * 1000)
    return {
        "ok": True,
        "action": "playing",
        "player": client.title,
        "now_playing": describe_item(item),
    }


@tool(
    "Play the next unwatched episode of a TV show, continuing a partially watched episode if there is one.",
    {
        "show": s("Show title."),
        "player": s("Player name from list_players."),
    },
    ["show"],
)
def play_next_episode(show, player=None):
    matches = [x for x in find_media(show, "show", 5) if x.type == "show"]
    if not matches:
        raise ToolError(f"No TV show matches {show!r}.")
    episode = next_unwatched_episode(matches[0])
    if episode is None:
        raise ToolError(f"{matches[0].title!r} has no episodes to play.")
    client = resolve_player(player)
    client.playMedia(episode)
    return {
        "ok": True,
        "action": "playing",
        "player": client.title,
        "now_playing": describe_item(episode),
    }


@tool(
    "Control playback on a player that is already playing something.",
    {
        "action": s(
            "One of: play, pause, stop, next, previous, step_forward, "
            "step_back, shuffle_on, shuffle_off, repeat_all, repeat_one, "
            "repeat_off. For a specific jump use 'seek' instead."),
        "player": s("Player name from list_players."),
    },
    ["action"],
)
def control(action, player=None):
    key = text(action).strip().lower()

    # Stopping is special: /status/sessions/terminate is a SERVER operation and
    # never touches Companion, so it works on clients that refuse every other
    # command - Amazon Fire TV included. Try it before giving up on them.
    if key == "stop":
        stopped = stop_session(player)
        if stopped:
            return stopped

    client = resolve_player(player)
    actions = {
        "play": client.play,
        "resume": client.play,
        "pause": client.pause,
        "stop": client.stop,
        "next": client.skipNext,
        "skip": client.skipNext,
        "previous": client.skipPrevious,
        "back": client.skipPrevious,
        "step_forward": client.stepForward,
        "step_back": client.stepBack,
        "shuffle_on": lambda **kw: client.setShuffle(1, **kw),
        "shuffle_off": lambda **kw: client.setShuffle(0, **kw),
        "repeat_off": lambda **kw: client.setRepeat(0, **kw),
        "repeat_all": lambda **kw: client.setRepeat(1, **kw),
        "repeat_one": lambda **kw: client.setRepeat(2, **kw),
    }
    if key not in actions:
        raise ToolError(
            f"Unknown action {action!r}.", valid_actions=sorted(set(actions))
        )
    actions[key](mtype="video")
    return {"ok": True, "action": key, "player": client.title}


def current_session(machine_identifier):
    """The session on a given player right now, or None."""
    for session in plex().sessions():
        for pl in getattr(session, "players", []) or []:
            if getattr(pl, "machineIdentifier", None) == machine_identifier:
                return session
    return None


@tool(
    "Jump to a position in whatever is currently playing. Give 'seconds' for "
    "an absolute position, or 'delta_seconds' to move relative to where it is "
    "now - negative to go back. 'skip ahead two minutes' is delta_seconds=120.",
    {
        "seconds": i("Absolute position in seconds from the start."),
        "delta_seconds": i(
            "Move this many seconds from the current position. Negative "
            "rewinds."),
        "player": s("Player name from list_players."),
    },
)
def seek(seconds=None, delta_seconds=None, player=None):
    if (seconds is None) == (delta_seconds is None):
        raise ToolError(
            "Give exactly one of seconds (absolute) or delta_seconds "
            "(relative)."
        )
    client = resolve_player(player)

    if delta_seconds is not None:
        session = current_session(client.machineIdentifier)
        if session is None:
            raise ToolError(
                f"{client.title!r} is not playing anything, so there is no "
                "current position to move from. Use seconds= for an absolute "
                "position, or start playback first.",
                player=client.title,
            )
        now_ms = int(getattr(session, "viewOffset", 0) or 0)
        target_ms = max(0, now_ms + int(delta_seconds) * 1000)
        duration = int(getattr(session, "duration", 0) or 0)
        if duration:
            target_ms = min(target_ms, duration - 1000)
        client.seekTo(target_ms, mtype="video")
        return {
            "ok": True,
            "player": client.title,
            "moved": f"{int(delta_seconds):+d}s",
            "from": ms_to_clock(now_ms),
            "position": ms_to_clock(target_ms),
        }

    target_ms = max(0, int(seconds) * 1000)
    client.seekTo(target_ms, mtype="video")
    return {
        "ok": True,
        "player": client.title,
        "position": ms_to_clock(target_ms),
    }


@tool(
    "Turn subtitles on or off, or switch the audio track, on whatever is "
    "playing. Something has to be playing - stream ids come from the active "
    "session.",
    {
        "player": s("Player name from list_players."),
        "subtitles": s(
            "'off' to disable, 'on' for the first available track, or a "
            "language name or code like 'English' / 'eng' / 'Spanish'."),
        "audio": s("Language name or code for the audio track, e.g. 'English'."),
    },
)
def set_streams(player=None, subtitles=None, audio=None):
    if not subtitles and not audio:
        raise ToolError("Nothing to change. Pass subtitles and/or audio.")

    client = resolve_player(player)
    session = current_session(client.machineIdentifier)
    if session is None:
        raise ToolError(
            f"{client.title!r} is not playing anything. Subtitle and audio "
            "tracks belong to a playing item, so start playback first.",
            player=client.title,
        )

    parts = [
        part
        for media in (getattr(session, "media", None) or [])
        for part in (getattr(media, "parts", None) or [])
    ]
    if not parts:
        raise ToolError(
            "The current session reports no media parts, so its tracks cannot "
            "be listed."
        )
    part = parts[0]

    def pick(streams, want):
        want = want.strip().lower()
        for attr in ("languageTag", "language", "languageCode", "title",
                     "displayTitle"):
            for stream in streams:
                value = (getattr(stream, attr, None) or "").lower()
                if value and (value == want or value.startswith(want)
                              or want in value):
                    return stream
        return None

    changed, available = {}, {}
    sub_streams = part.subtitleStreams()
    audio_streams = part.audioStreams()

    if subtitles is not None:
        want = subtitles.strip().lower()
        available["subtitles"] = [
            {"id": x.id, "language": getattr(x, "language", None),
             "codec": getattr(x, "codec", None),
             "forced": bool(getattr(x, "forced", False))}
            for x in sub_streams
        ]
        if want in ("off", "none", "disable", "disabled", "no"):
            client.setSubtitleStream(0, mtype="video")
            changed["subtitles"] = "off"
        elif not sub_streams:
            raise ToolError(
                f"{getattr(session, 'title', 'this item')} has no subtitle "
                "tracks at all, so subtitles cannot be turned on.",
                item=getattr(session, "title", None),
            )
        else:
            stream = (sub_streams[0] if want in ("on", "yes", "enable")
                      else pick(sub_streams, want))
            if stream is None:
                raise ToolError(
                    f"No subtitle track matches {subtitles!r}.",
                    available=available["subtitles"],
                )
            client.setSubtitleStream(stream.id, mtype="video")
            changed["subtitles"] = (
                getattr(stream, "language", None)
                or getattr(stream, "displayTitle", None)
                or f"stream {stream.id}"
            )

    if audio is not None:
        available["audio"] = [
            {"id": x.id, "language": getattr(x, "language", None),
             "codec": getattr(x, "codec", None),
             "channels": getattr(x, "channels", None)}
            for x in audio_streams
        ]
        stream = pick(audio_streams, audio)
        if stream is None:
            raise ToolError(
                f"No audio track matches {audio!r}.",
                available=available["audio"],
            )
        client.setAudioStream(stream.id, mtype="video")
        changed["audio"] = (
            getattr(stream, "language", None) or f"stream {stream.id}"
        )

    return {
        "ok": True,
        "player": client.title,
        "playing": getattr(session, "title", None),
        "changed": changed,
        "available": available,
        "note": (
            "Not every client honours a stream switch mid-playback; if nothing "
            "changes on screen, the client ignored it."
        ),
    }


@tool(
    "Set player volume (0-100). Not every client supports this.",
    {
        "level": i("Volume 0-100."),
        "player": s("Player name from list_players."),
    },
    ["level"],
)
def set_volume(level, player=None):
    level = max(0, min(100, int(level)))
    client = resolve_player(player)
    client.setVolume(level, mtype="video")
    return {"ok": True, "player": client.title, "volume": level}


@tool("Show what is playing right now across all players, and how far in it is.")
def now_playing():
    sessions = []
    for session in plex().sessions():
        info = describe_item(session)
        players = getattr(session, "players", []) or []
        info["player"] = players[0].title if players else None
        info["state"] = players[0].state if players else None
        info["position"] = ms_to_clock(getattr(session, "viewOffset", 0))
        info["user"] = (getattr(session, "usernames", []) or [None])[0]
        sessions.append(info)
    return {"ok": True, "count": len(sessions), "sessions": sessions}


@tool(
    "Show the On Deck list - partially watched and next-up items. Good for 'put on the thing I was watching'.",
    {"limit": i("Maximum items.", 10)},
)
def on_deck(limit=10):
    items = plex().library.onDeck()[: int(limit)]
    return {"ok": True, "count": len(items), "items": [describe_item(x) for x in items]}


@tool(
    "Show recently added items.",
    {
        "limit": i("Maximum items.", 10),
        "library": s("Optional library name to restrict to."),
    },
)
def recently_added(limit=10, library=None):
    p = plex()
    if library:
        items = p.library.section(library).recentlyAdded(maxresults=int(limit))
    else:
        items = p.library.recentlyAdded()[: int(limit)]
    return {"ok": True, "count": len(items), "items": [describe_item(x) for x in items]}


@tool(
    "Scan a library for new files and report scan status. Run this after "
    "adding media - until it runs, new files are not in Plex and every other "
    "tool here will correctly say they are missing.",
    {
        "library": s("Library to scan. Default: every library."),
        "refresh_metadata": b(
            "Also re-download metadata for everything in the library. This is "
            "heavy - it re-queries the agent for every item and can run for "
            "hours on a large library. Leave off unless artwork or metadata is "
            "broken across the board; for one bad item use refresh_item."),
        "wait_seconds": i(
            "Poll for up to this long and report whether the scan finished. "
            "Default 0 - return immediately and let it run.", 0),
    },
)
def refresh_library(library=None, refresh_metadata=False, wait_seconds=0):
    sections = resolve_sections(library)
    started = []
    for section in sections:
        try:
            section.update()  # scan for new files
            if refresh_metadata:
                section.refresh()
            started.append(section.title)
        except Exception as exc:
            raise ToolError(
                f"Plex refused the scan on {section.title!r}: "
                f"{type(exc).__name__}: {exc}",
                hint="A server-only token can read but not trigger scans.",
            )

    # Anything cached is about to be wrong.
    invalidate_library_cache()

    result = {
        "ok": True,
        "scanned": started,
        "metadata_refresh": bool(refresh_metadata),
    }

    wait_seconds = max(0, int(wait_seconds or 0))
    if wait_seconds:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(2)
            if not any(_is_refreshing(x) for x in sections):
                result["finished"] = True
                break
        else:
            result["finished"] = False
    result["status"] = [
        {
            "library": x.title,
            "scanning": _is_refreshing(x),
            "items": x.totalSize,
            "last_updated": str(getattr(x, "updatedAt", None)),
        }
        for x in resolve_sections(library)
    ]
    if not result.get("finished", True):
        result["note"] = (
            "Still scanning. Item counts above are mid-scan and will grow. "
            "Call again to check."
        )
    else:
        result["note"] = (
            "A scan only picks up files that are already in the library "
            "folders. If something is still missing afterwards, the file is "
            "not where Plex is looking."
        )
    return result


def _is_refreshing(section):
    try:
        section.reload()
        return bool(getattr(section, "refreshing", False))
    except Exception:
        return False


@tool(
    "Re-download metadata for one item - the fix for a wrong poster, a missing "
    "summary, or an episode Plex matched to the wrong show.",
    {
        "rating_key": s("rating_key of the item, from any search result."),
        "query": s("Title, if you do not have a rating_key."),
    },
)
def refresh_item(rating_key=None, query=None):
    if rating_key:
        item = get_by_rating_key(rating_key)
    elif query:
        matches = find_media(query, None, 5)
        if not matches:
            raise ToolError(f"Nothing matches {query!r}.")
        if len(matches) > 1 and (
            matches[0].title or ""
        ).lower() != query.strip().lower():
            raise ToolError(
                f"{query!r} matches several items; pass a rating_key so the "
                "right one gets refreshed.",
                candidates=[describe_item(x) for x in matches[:5]],
            )
        item = matches[0]
    else:
        raise ToolError("Pass rating_key or query.")

    item.refresh()
    invalidate_library_cache()
    return {
        "ok": True,
        "refreshed": describe_item(item),
        "note": (
            "Plex re-queries its metadata agent in the background; the new "
            "data lands within a few seconds. If the item stays wrong, it is "
            "matched to the wrong entry and needs fixing by hand in Plex."
        ),
    }


@tool(
    "Mark something watched or unwatched. Use for repairing watch state - a "
    "film someone watched elsewhere, or an episode Plex marked played by "
    "accident.",
    {
        "rating_key": s("rating_key of the item. Safest way to name it."),
        "query": s("Title, if you do not have a rating_key. Must match exactly "
                   "one item or the call is refused."),
        "watched": b("True to mark played, false to mark unplayed.", True),
    },
)
def mark_watched(rating_key=None, query=None, watched=True):
    if rating_key:
        item = get_by_rating_key(rating_key)
    elif query:
        matches = find_media(query, None, 5)
        if not matches:
            raise ToolError(f"Nothing matches {query!r}.")
        exact = [x for x in matches
                 if (x.title or "").strip().lower() == query.strip().lower()]
        if len(exact) == 1:
            item = exact[0]
        elif len(matches) == 1:
            item = matches[0]
        else:
            # Marking the wrong thing watched quietly corrupts On Deck and the
            # next-episode logic, so ambiguity is refused rather than guessed.
            raise ToolError(
                f"{query!r} matches several items. Pass a rating_key.",
                candidates=[describe_item(x) for x in matches[:5]],
            )
    else:
        raise ToolError("Pass rating_key or query.")

    if watched:
        item.markPlayed()
    else:
        item.markUnplayed()
    invalidate_library_cache()
    return {
        "ok": True,
        "action": "marked watched" if watched else "marked unwatched",
        "item": describe_item(item),
    }


@tool(
    "What has actually been watched recently, newest first. Use this to ground "
    "recommendations in real viewing rather than in the library's watched flag.",
    {
        "limit": i("Maximum entries. Default 25.", 25),
        "days": i("Only look back this many days."),
        "library": s("Restrict to one library."),
    },
)
def watch_history(limit=25, days=None, library=None):
    p = plex()
    limit = max(1, int(limit or 25))
    kwargs = {"maxresults": limit}
    if days:
        kwargs["mindate"] = datetime.datetime.now() - datetime.timedelta(
            days=int(days)
        )
    if library:
        kwargs["librarySectionID"] = resolve_sections(library)[0].key

    entries = []
    for row in p.history(**kwargs):
        entry = {
            "title": getattr(row, "title", None),
            "type": getattr(row, "type", None),
            "watched_at": str(getattr(row, "viewedAt", None)),
        }
        show = getattr(row, "grandparentTitle", None)
        if show:
            entry["show"] = show
            entry["season"] = getattr(row, "parentIndex", None)
            entry["episode"] = getattr(row, "index", None)
        entries.append(entry)

    return {
        "ok": True,
        "count": len(entries),
        "history": entries,
        "note": (
            "History is per Plex account and only covers playback this server "
            "saw. An empty result does not mean nothing was watched."
        ),
    }


@tool("List playlists on the server.")
def list_playlists():
    return {
        "ok": True,
        "playlists": [
            {"title": pl.title, "type": pl.playlistType, "items": len(pl.items())}
            for pl in plex().playlists()
        ],
    }


@tool(
    "Play a playlist on a player.",
    {
        "name": s("Playlist name."),
        "player": s("Player name from list_players."),
        "shuffle": b("Shuffle the playlist."),
    },
    ["name"],
)
def play_playlist(name, player=None, shuffle=False):
    playlists = [pl for pl in plex().playlists() if pl.title.lower() == name.strip().lower()]
    if not playlists:
        playlists = [pl for pl in plex().playlists() if name.strip().lower() in pl.title.lower()]
    if not playlists:
        raise ToolError(
            f"No playlist matches {name!r}.",
            available=[pl.title for pl in plex().playlists()],
        )
    client = resolve_player(player)
    client.playMedia(playlists[0], shuffle=1 if shuffle else 0)
    return {"ok": True, "action": "playing", "player": client.title, "playlist": playlists[0].title}


@tool(
    "Build a playlist from specific items - a movie night line-up, a run of "
    "episodes, a themed set assembled from several searches.",
    {
        "title": s("Name for the playlist."),
        "rating_keys": s(
            "The items to put in it: rating_key values from search, discover "
            "or library_export, comma-separated and in the order you want "
            "them played."),
        "replace_existing": b(
            "If a playlist with this name already exists, delete it first. "
            "Otherwise an existing name is an error."),
    },
    ["title", "rating_keys"],
)
def create_playlist(title, rating_keys, replace_existing=False):
    p = plex()
    keys = [k.strip() for k in str(rating_keys).replace("\n", ",").split(",")
            if k.strip()]
    if not keys:
        raise ToolError("No rating_keys given.")

    items, bad = [], []
    for key in keys:
        try:
            items.append(get_by_rating_key(key))
        except Exception:
            bad.append(key)
    if not items:
        raise ToolError(
            "None of those rating_keys resolved to an item.",
            unresolved=bad,
            hint="rating_key values come from search, discover or "
                 "library_export - they are not titles.",
        )

    existing = [x for x in p.playlists()
                if x.title.strip().lower() == title.strip().lower()]
    if existing:
        if not replace_existing:
            raise ToolError(
                f"A playlist named {title!r} already exists with "
                f"{len(existing[0].items())} items.",
                hint="Pass replace_existing=true to overwrite it, or pick "
                     "another name.",
            )
        for old in existing:
            old.delete()

    playlist = p.createPlaylist(title, items=items)
    return {
        "ok": True,
        "playlist": playlist.title,
        "items": len(items),
        "unresolved": bad,
        "contents": [describe_item(x) for x in items[:20]],
        "note": "Play it with play_playlist.",
    }


# ---------------------------------------------------------------------------
# Dispatch - shared by CLI and MCP
# ---------------------------------------------------------------------------


def call_tool(name, args):
    """Run a tool. Never raises: failures come back as ok:false with the real error."""
    entry = TOOLS.get(name)
    if entry is None:
        return {"ok": False, "error": f"Unknown tool {name!r}", "available_tools": sorted(TOOLS)}
    try:
        signature = inspect.signature(entry["fn"])
        accepted = {k: v for k, v in (args or {}).items() if k in signature.parameters}
        rejected = sorted(set((args or {}) ) - set(accepted))
        result = entry["fn"](**accepted)
        if rejected:
            result["ignored_arguments"] = rejected
        return result
    except ToolError as exc:
        payload = {"ok": False, "error": str(exc)}
        payload.update(exc.extra)
        return payload
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "Report this error verbatim. Do not retry with altered arguments "
                "until the cause is understood."
            ),
        }


# ---------------------------------------------------------------------------
# MCP stdio server (JSON-RPC 2.0, newline-delimited)
# ---------------------------------------------------------------------------


_stdout = sys.stdout


def serve():
    # stdout is the JSON-RPC transport. One stray print() anywhere - here, in a
    # dependency, in a warning - corrupts the stream and the handshake fails
    # with no useful error. Hold the real handle for emit() and point sys.stdout
    # at stderr so accidental writes are merely logged instead of fatal.
    global _stdout
    _stdout = sys.stdout
    sys.stdout = sys.stderr

    log(f"serving {len(TOOLS)} tools over stdio; PLEX_URL={PLEX_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON on stdin: {exc}")
            continue

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        response = None

        try:
            if method == "initialize":
                response = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method == "tools/list":
                response = {
                    "tools": [
                        {
                            "name": name,
                            "description": entry["description"],
                            "inputSchema": entry["inputSchema"],
                        }
                        for name, entry in TOOLS.items()
                    ]
                }
            elif method == "tools/call":
                result = call_tool(params.get("name"), params.get("arguments") or {})
                response = {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
                    "isError": not result.get("ok", False),
                }
            elif method == "ping":
                response = {}
            elif method and method.startswith("notifications/"):
                continue  # notifications take no reply
            else:
                if req_id is not None:
                    emit({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    })
                continue
        except Exception:
            log(traceback.format_exc())
            if req_id is not None:
                emit({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": traceback.format_exc(limit=2)},
                })
            continue

        if req_id is not None:
            emit({"jsonrpc": "2.0", "id": req_id, "result": response})


def emit(payload):
    _stdout.write(json.dumps(payload, default=str) + "\n")
    _stdout.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_cli_args(argv):
    """key=value pairs, with light coercion so quoting stays simple."""
    args = {}
    for token in argv:
        if "=" not in token:
            raise SystemExit(f"Arguments must be key=value, got {token!r}")
        key, _, value = token.partition("=")
        if value.lower() in ("true", "false"):
            args[key] = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            args[key] = int(value)
        else:
            args[key] = value
    return args


def usage():
    print("Plex MCP server / CLI\n")
    print("  python plex_mcp_server.py serve            # run as an MCP server")
    print("  python plex_mcp_server.py <tool> k=v ...   # run one tool directly\n")
    print("Tools:")
    for name, entry in TOOLS.items():
        params = ", ".join(entry["inputSchema"]["properties"]) or "-"
        print(f"  {name:<20} {params}")
        print(f"  {'':<20} {entry['description']}")
    print(f"\nPLEX_URL={PLEX_URL}  PLEX_TOKEN={'set' if PLEX_TOKEN else 'MISSING'}")


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage()
        return
    if argv[0] == "serve":
        serve()
        return
    result = call_tool(argv[0], parse_cli_args(argv[1:]))
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
