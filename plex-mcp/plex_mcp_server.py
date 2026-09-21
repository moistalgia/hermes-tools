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
import urllib.parse
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

def normalize_spoken(value):
    """Fold a room or device name to a comparison key.

    Speech and device names disagree on articles, possessives and punctuation:
    "andie's office", "Andies Office" and "the andie office" are one room, and
    "Roku Express 4K+" arrives without the plus about half the time. Folding
    both sides of every comparison is cheaper than enumerating the variants.
    """
    text = str(value or "").lower().replace("'", "").replace("’", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def parse_aliases(raw):
    """Build the spoken-name map from PLEX_ALIASES.

    Two value forms, because one room is said more than one way and a config
    that rejects the other way is a bug in the config, not in the speech:

        "theater": "Streaming Stick 4K"
        "theater": ["Streaming Stick 4K", "theatre", "movie room", "basement"]

    The first element is the target; the rest are extra spellings for the same
    room. A target may be a display name or a machine_identifier - prefer the
    identifier, since it is the only key a device cannot change out from under
    you. Roku boxes in particular report the retail box name and cannot be
    renamed from here at all.

    A room whose value is neither a string nor a list is skipped with a warning
    rather than taking the other rooms down with it: one typo in config should
    cost one room, not every room in the house.

    Returns (spoken -> target, target key -> room label).
    """
    spoken, rooms = {}, {}
    for room, value in (raw or {}).items():
        if isinstance(value, str):
            names = [value]
        elif isinstance(value, (list, tuple)):
            names = list(value)
        else:
            print(f"[plex-mcp] PLEX_ALIASES[{room!r}] must be a name or a list "
                  f"of names, not {type(value).__name__}; skipping that room",
                  file=sys.stderr)
            continue
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            continue
        target, extra = names[0], names[1:]
        label = str(room).strip()
        rooms[normalize_spoken(target)] = label
        for said in [label] + extra:
            key = normalize_spoken(said)
            if key:
                spoken[key] = target
    return spoken, rooms


# {"theater": ["Streaming Stick 4K", "movie room"]} - maps what people say out
# loud onto what Plex calls the device. Plex names are frequently useless
# ("unknown", "Sleepy"), and a room name outlives the hardware in it.
try:
    _raw_aliases = json.loads(os.environ.get("PLEX_ALIASES", "{}"))
except (ValueError, AttributeError):
    _raw_aliases = {}
    print("[plex-mcp] PLEX_ALIASES is not valid JSON; ignoring it", file=sys.stderr)

try:
    PLEX_ALIASES, PLEX_ROOMS = parse_aliases(_raw_aliases)
except AttributeError:
    PLEX_ALIASES, PLEX_ROOMS = {}, {}
    print("[plex-mcp] PLEX_ALIASES must be a JSON object of room to player; "
          "ignoring it", file=sys.stderr)


def room_of(name=None, machine_identifier=None):
    """The configured room label for a device, by identifier then by name."""
    for key in (machine_identifier, name):
        label = PLEX_ROOMS.get(normalize_spoken(key)) if key else None
        if label:
            return label
    return None


def said_name(player):
    """What to call a session's player out loud: its room, else its own name."""
    if player is None:
        return "?"
    title = getattr(player, "title", None)
    return room_of(title, getattr(player, "machineIdentifier", None)) or title or "?"

# Roku's External Control Protocol. Open on the LAN, no auth, and answers even
# when the Plex app is closed - which is what lets us wake a device instead of
# telling the user to go press buttons.
ROKU_ECP_PORT = 8060
ROKU_PLEX_CHANNEL_ID = os.environ.get("ROKU_PLEX_CHANNEL_ID", "13535")

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "plex"
SERVER_VERSION = "1.6.0"


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

    # Keep the whole player, not just its state: a session is the only place
    # some devices appear at all, and rebuilding an entry for one needs a name.
    streaming = {}
    for session in p.sessions():
        for pl in getattr(session, "players", []) or []:
            mid = getattr(pl, "machineIdentifier", None)
            if not mid:
                continue
            streaming[mid] = {
                "state": getattr(pl, "state", None),
                "name": getattr(pl, "title", None),
                "product": getattr(pl, "product", None),
                "platform": getattr(pl, "platform", None),
            }

    out = []
    seen = set()
    for d in account_devices():
        mid = d["machine_identifier"]
        seen.add(mid)
        entry = dict(d)
        entry["streaming_now"] = (streaming.get(mid) or {}).get("state")
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
        seen.add(mid)
        out.append({
            "name": c.title, "product": getattr(c, "product", None),
            "platform": getattr(c, "platform", None), "machine_identifier": mid,
            "provides": ["player"], "connections": [], "last_seen": None,
            "advertises_player": True, "reachable": True, "controllable": True,
            "route": "server", "streaming_now": (streaming.get(mid) or {}).get("state"),
            "status": "ready (registered with the Plex server)", "relevant": True,
        })

    # And anything that only a live session knows about. A device signed in to
    # a different Plex user streams from this server without ever appearing on
    # this token's plex.tv device list or in /clients, so without this pass it
    # exists in now_playing and nowhere else - which reads as the two tools
    # contradicting each other rather than as one account boundary.
    for mid, info in streaming.items():
        if mid in seen:
            continue
        out.append({
            "name": info["name"] or "unknown", "product": info["product"],
            "platform": info["platform"], "machine_identifier": mid,
            "provides": [], "connections": [], "last_seen": None,
            "advertises_player": False, "reachable": False, "controllable": False,
            "route": None, "streaming_now": info["state"],
            "status": (
                "streaming now, but not registered as a controllable client on "
                "this account - usually a device signed in as a different Plex "
                "user. It can be seen and named, not driven. Reporting that is "
                "the answer."),
            "relevant": True,
        })

    for entry in out:
        entry["room"] = room_of(entry.get("name"), entry.get("machine_identifier"))

    # Room first when it is known: the map is the whole reason a caller says
    # "theater" rather than "Streaming Stick 4K", and grouping by it makes a
    # missing map entry obvious at a glance.
    out.sort(key=lambda d: (not d["controllable"], d["room"] or "~",
                            (d["name"] or "").lower()))
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
    # Every tool reports client.title back as "player", so the mapped room name
    # is what the agent echoes - "playing on the theater", not "on Streaming
    # Stick 4K". Commands route on machineIdentifier, so this is display only.
    client.title = entry.get("room") or entry["name"]
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
                players=[
                    {"name": d["name"], "room": d["room"], "status": d["status"]}
                    for d in notable
                ],
            )
        raise ToolError(
            "No player specified and more than one is available.",
            available_players=[d["room"] or d["name"] for d in usable],
        )

    want = normalize_spoken(name)
    # A room name ("theater") is what gets said out loud; translate before
    # matching so aliases work with every downstream match rule.
    want = normalize_spoken(PLEX_ALIASES.get(want, want))

    def player_name(d):
        return normalize_spoken(d.get("name"))

    def haystack(d):
        # Some clients report a useless name - the Fire TV registers as
        # literally "unknown" - so product and platform have to be searchable
        # or there is no way to refer to the device at all.
        return normalize_spoken(" ".join(filter(None, [
            d.get("name"), d.get("product"), d.get("platform"), d.get("room"),
        ])))

    if not want:
        raise ToolError(
            f"{name!r} does not contain anything to match a player on.",
            available_players=[d["room"] or d["name"] for d in usable],
        )

    for match in (
        # Identifier first: it is the only key that survives a device being
        # renamed, so a map pinned to one always beats a display-name collision.
        # Folded on both sides because plenty of clients use a hyphenated UUID.
        lambda d: normalize_spoken(d.get("machine_identifier")) == want,
        lambda d: normalize_spoken(d.get("room")) == want if d.get("room") else False,
        lambda d: player_name(d) == want,
        lambda d: player_name(d).startswith(want),
        lambda d: want in player_name(d),
        lambda d: want in haystack(d),
    ):
        matches = [d for d in players if match(d)]
        if matches:
            break

    if not matches:
        raise ToolError(
            f"No player matches {name!r}.",
            available_players=[d["room"] or d["name"] for d in usable],
            all_known_players=[
                {"name": d["name"], "room": d["room"], "status": d["status"]}
                for d in notable
            ],
        )
    if len(matches) > 1:
        usable_matches = [d for d in matches if d["controllable"]]
        if len(usable_matches) != 1:
            raise ToolError(
                f"{name!r} is ambiguous.",
                candidates=[d["room"] or d["name"] for d in matches],
            )
        matches = usable_matches

    chosen = matches[0]
    said = chosen["room"] or chosen["name"]
    if not chosen["controllable"]:
        # A Roku with its app closed is a solvable problem, not a refusal.
        if chosen["advertises_player"] and _ecp(chosen):
            log(f"{said!r} is asleep; launching Plex on it")
            woke, detail = wake_plex(chosen)
            if woke:
                refreshed = [
                    d for d in discover_players()
                    if d["machine_identifier"] == chosen["machine_identifier"]
                ]
                if refreshed and refreshed[0]["controllable"]:
                    return build_client(refreshed[0])
            raise ToolError(
                f"{said!r} could not be woken: {detail}",
                player=said,
                controllable_players=[d["room"] or d["name"] for d in usable],
            )
        raise ToolError(
            f"{said!r} cannot be controlled: {chosen['status']}",
            player=said,
            product=chosen.get("product"),
            streaming_now=chosen.get("streaming_now"),
            controllable_players=[d["room"] or d["name"] for d in usable],
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

    want = normalize_spoken(player_name)
    want = normalize_spoken(PLEX_ALIASES.get(want, want))

    def player_of(session):
        players = getattr(session, "players", []) or []
        return players[0] if players else None

    if want:
        chosen = None
        for session in sessions:
            pl = player_of(session)
            if not pl:
                continue
            mid = getattr(pl, "machineIdentifier", "") or ""
            # Same ladder as resolve_player: identifier, then room, then the
            # display name. A session-only device has no other way to be named.
            hay = normalize_spoken(" ".join(filter(None, [
                getattr(pl, "title", ""), getattr(pl, "product", ""),
                room_of(getattr(pl, "title", None), mid) or "",
            ])))
            if normalize_spoken(mid) == want or want in hay:
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
                {"player": said_name(player_of(x)), "title": x.title}
                for x in sessions
            ],
        )

    pl = player_of(chosen)
    chosen.stop(reason="Stopped by Hermes")
    return {
        "ok": True,
        "action": "stop",
        "player": said_name(pl),
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
        "players": [d["room"] or d["name"] for d in players if d["controllable"]],
        "players_unavailable": [
            {"name": d["room"] or d["name"], "reason": d["status"]}
            for d in players if not d["controllable"] and d.get("relevant")
        ],
        "active_sessions": len(p.sessions()),
    }


@tool(
    "List every known Plex player and whether it can be controlled right now. "
    "Playback is NOT required for a device to appear here. Pass a player's "
    "'room' as the 'player' argument elsewhere when it has one, otherwise its "
    "'name' verbatim.",
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
        "controllable": [d["room"] or d["name"] for d in usable],
        "unmapped": [
            d["name"] for d in players if not d["room"] and d.get("relevant")
        ],
        "unavailable": [
            {"name": d["name"], "room": d["room"], "reason": d["status"]}
            for d in blocked
        ],
        "note": (
            "A player with controllable=false cannot be driven. When the reason "
            "says the app never advertises itself as a player, that is final - "
            "report it and stop. No argument variation or alternate API fixes it. "
            "Prefer a player's room name when talking to the user; anything in "
            "'unmapped' has no room configured in PLEX_ALIASES yet."
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
        "player": s("Room name, or a player name from list_players. Optional if "
                    "only one player exists."),
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
        "player": s("Room name, or a player name from list_players."),
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
        "player": s("Room name, or a player name from list_players."),
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
        "player": s("Room name, or a player name from list_players."),
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
        "player": s("Room name, or a player name from list_players."),
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
        "player": s("Room name, or a player name from list_players."),
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
        "player": s("Room name, or a player name from list_players."),
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
        pl = players[0] if players else None
        # machine_identifier is carried so this joins cleanly against
        # list_players. Display names are user-settable and already inconsistent
        # across this house; the identifier is the only stable key between the
        # two tools.
        info["machine_identifier"] = getattr(pl, "machineIdentifier", None)
        info["player"] = said_name(pl) if pl else None
        info["device_name"] = getattr(pl, "title", None)
        info["state"] = getattr(pl, "state", None)
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

# ---------------------------------------------------------------------------
# Metadata editing
#
# The tools above read the library; these three change it. Everything here
# exists because a wrong Plex match is a two-part problem: the agent has to be
# able to see the current state exactly, and it has to be unable to destroy the
# rest of the record while fixing the part that is wrong.
#
# Three rules the rest of this section implements:
#
#   Arrays replace, they do not append. Appending is what produces a movie
#   carrying ["Horror", "Horror", "horror"] after three corrections, and it
#   makes removing a wrong genre impossible.
#
#   A write requires confirm=true. Without it the call returns the diff it
#   would have made, so the normal shape of a correction is propose, read,
#   confirm - not "hope the model got it right the first time".
#
#   Nothing reports success on the strength of an HTTP 200. Plex accepts edits
#   it then declines to apply, so every write is read back and only the
#   readback sets verified.
#
# Edited fields are locked. An unlocked correction is one the next metadata
# refresh is free to overwrite with the same bad provider data that caused the
# correction, and nobody checks a fix twice. This is also why refresh is not
# run automatically anywhere in here: refresh_item exists, it is a separate
# call, and it is a reasonable thing to do *before* an edit and a destructive
# thing to do after one.
# ---------------------------------------------------------------------------

# Our argument name -> the Plex field name. Plex's names are inconsistent
# enough (titleSort, originalTitle) that spelling them at every call site is
# how the wrong one gets written.
EDITABLE_FIELDS = {
    "title": "title",
    "year": "year",
    "original_title": "originalTitle",
    "sort_title": "titleSort",
    "summary": "summary",
}

# Our argument name -> (plexapi attribute, Plex tag parameter). The parameter
# is singular and the attribute is plural; Plex's edit endpoint ignores a
# plural one without complaining, which looks exactly like a write that worked.
EDITABLE_TAGS = {
    "genres": ("genres", "genre"),
    "labels": ("labels", "label"),
    "collections": ("collections", "collection"),
    "countries": ("countries", "country"),
}

# Editing is a library-item operation. A player, a session or a playlist has a
# ratingKey too, and an edit aimed at one of those fails in a way that reads
# like a Plex outage rather than like a wrong argument.
EDITABLE_TYPES = ("movie", "show", "season", "episode", "artist", "album", "track")

BATCH_LIMIT = 25


def tag_values(item, attribute):
    return [str(t.tag) for t in (getattr(item, attribute, None) or [])
            if getattr(t, "tag", None)]


def locked_fields(item):
    return sorted(f.name for f in (getattr(item, "fields", None) or [])
                  if getattr(f, "locked", False) and getattr(f, "name", None))


def media_parts(item):
    """File paths, reported and never touched.

    Here so a correction can be checked against what is actually on disk - a
    title like "L Ultimo Esorcismo" is a filename problem before it is a
    metadata problem - and so that checking does not require a second tool that
    can move files.
    """
    out = []
    for medium in (getattr(item, "media", None) or []):
        for part in (getattr(medium, "parts", None) or []):
            out.append({
                "file": getattr(part, "file", None),
                "size": getattr(part, "size", None),
                "container": getattr(part, "container", None),
            })
    return out


def raw_attributes(item):
    """The item's own XML attributes, minus anything token-shaped.

    Useful for the fields this tool deliberately does not edit - guid, the
    matched agent, originallyAvailableAt - because those are what tell you
    whether a bad match needs a correction or a rematch. The token filter is
    belt and braces: these attributes do not carry one today.
    """
    attrib = getattr(getattr(item, "_data", None), "attrib", None) or {}
    return {k: v for k, v in dict(attrib).items() if "token" not in k.lower()}


def metadata_snapshot(item, include_raw=False):
    """The exact editable state of one item, in this server's argument names."""
    out = {
        "rating_key": str(getattr(item, "ratingKey", "")),
        "type": getattr(item, "type", None),
        "library": getattr(item, "librarySectionTitle", None),
        "title": getattr(item, "title", None),
        "year": getattr(item, "year", None),
        "original_title": getattr(item, "originalTitle", None),
        "sort_title": getattr(item, "titleSort", None),
        "summary": getattr(item, "summary", None),
        "locked_fields": locked_fields(item),
    }
    for name, (attribute, _param) in EDITABLE_TAGS.items():
        out[name] = tag_values(item, attribute)
    if include_raw:
        out["media_parts"] = media_parts(item)
        out["raw_metadata"] = raw_attributes(item)
    return out


def resolve_editable_item(rating_key=None, query=None):
    """One library item, or a refusal that names the alternatives.

    A title is allowed to *find* an item and never to be the only thing
    standing behind a write: "Black Sunday" is two films thirteen years apart
    and both are horror, so a fuzzy pick that lands on the wrong one produces a
    confident, wrong, locked correction. Ambiguity comes back as candidates
    with their rating keys, which is the thing the caller actually needs.
    """
    if rating_key not in (None, ""):
        try:
            item = get_by_rating_key(rating_key)
        except Exception as exc:
            raise ToolError(
                f"No item with rating_key {rating_key!r}: {exc}",
                error_code="not_found",
            )
        kind = getattr(item, "type", None)
        if kind not in EDITABLE_TYPES:
            raise ToolError(
                f"rating_key {rating_key!r} is a {kind!r}, which has no "
                "editable library metadata.",
                error_code="invalid_request",
            )
        if not item.isFullObject():
            try:
                item.reload()
            except Exception:
                pass
        return item

    wanted = text(query).strip()
    if not wanted:
        raise ToolError("Pass rating_key or query.", error_code="invalid_request")

    # "Torso (1973)" is how a person writes a title that needs its year to be
    # unambiguous, and that is exactly the case where guessing is worst.
    title_part, year_part = split_title_year(wanted)
    matches = [m for m in find_media(title_part, None, 10)
               if getattr(m, "type", None) in EDITABLE_TYPES]
    if not matches:
        raise ToolError(f"Nothing in the library matches {wanted!r}.",
                        error_code="not_found")

    exact = [m for m in matches
             if normalize_title(getattr(m, "title", "")) == normalize_title(title_part)]
    narrowed = exact or matches
    if year_part:
        by_year = [m for m in narrowed if getattr(m, "year", None) == year_part]
        if by_year:
            narrowed = by_year

    if len(narrowed) == 1:
        item = narrowed[0]
        if not item.isFullObject():
            try:
                item.reload()
            except Exception:
                pass
        return item

    raise ToolError(
        f"{wanted!r} matches {len(narrowed)} items. Pass the rating_key of the "
        "one you mean - a title and year collision is the case this refuses to "
        "guess at.",
        error_code="ambiguous_match",
        candidates=[describe_item(m) for m in narrowed[:10]],
    )


def normalize_tag_list(value, field):
    """A tag array, however it arrived, deduped case-insensitively in order."""
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw_text = text(value).strip()
        if not raw_text:
            raw = []
        elif raw_text.startswith("["):
            try:
                raw = json.loads(raw_text)
            except ValueError:
                raise ToolError(
                    f"{field} looked like a JSON array but did not parse.",
                    error_code="invalid_request",
                )
        else:
            raw = raw_text.split(",")
    out, seen = [], set()
    for entry in raw:
        entry = text(entry).strip()
        if entry and entry.lower() not in seen:
            seen.add(entry.lower())
            out.append(entry)
    return out


def tag_params(param, values, remove=False):
    """Plex's repeated tag parameters for one tag type.

    Setting is indexed - genre[0].tag.tag, genre[1].tag.tag - and removal is a
    single comma-joined minus parameter. Values in the removal are quoted here
    and again by the query builder on the way out; that double encoding is what
    Plex expects on the minus form, and dropping it loses every tag containing
    an ampersand.
    """
    if remove:
        return {
            f"{param}[].tag.tag-": ",".join(
                urllib.parse.quote(str(v)) for v in values),
            f"{param}.locked": 1,
        }
    out = {f"{param}.locked": 1}
    for index, value in enumerate(values):
        out[f"{param}[{index}].tag.tag"] = value
    return out


def same_tags(a, b):
    return sorted(x.lower() for x in a) == sorted(x.lower() for x in b)


def plan_metadata_update(item, spec):
    """Work out the diff and the writes, or refuse. Touches nothing.

    Raises ToolError for anything the caller got wrong, so a batch can validate
    every target before it writes any of them.
    """
    before = metadata_snapshot(item)
    changes, field_edits, tag_edits = {}, {}, []
    already_locked = set(before["locked_fields"])

    for name, plex_field in EDITABLE_FIELDS.items():
        supplied = spec.get(name)
        if supplied is None:
            continue
        if name == "year":
            try:
                value = int(supplied)
            except (TypeError, ValueError):
                raise ToolError(f"year must be a number, got {supplied!r}.",
                                error_code="invalid_request")
            if not 1870 <= value <= 2100:
                raise ToolError(f"year {value} is not a plausible release year.",
                                error_code="invalid_request")
        else:
            value = text(supplied).strip()
            if not value:
                # An empty string here is almost always a template that did not
                # get filled in, and writing it would blank a good title.
                raise ToolError(
                    f"{name} was supplied as an empty string. Omit the field to "
                    "leave it alone.",
                    error_code="invalid_request",
                )
        if before.get(name) != value:
            changes[name] = {"before": before.get(name), "after": value}
        elif plex_field in already_locked:
            continue  # right value, already pinned; nothing to write
        field_edits[f"{plex_field}.value"] = value
        field_edits[f"{plex_field}.locked"] = 1

    for name, (_attribute, param) in EDITABLE_TAGS.items():
        supplied = spec.get(name)
        cleared = bool(spec.get(f"clear_{name}"))
        if supplied is None and not cleared:
            continue
        desired = normalize_tag_list(supplied, name) if supplied is not None else []
        if cleared and desired:
            raise ToolError(
                f"clear_{name} was set together with a non-empty {name} list. "
                "Pick one.",
                error_code="invalid_request",
            )
        if not desired and not cleared:
            # The payload that erases a record is an empty array nobody meant
            # to send, so erasing has to be spelled out.
            raise ToolError(
                f"{name} was supplied as an empty list, which would erase every "
                f"{name[:-1]} on the item. Pass clear_{name}=true if that is "
                "what you mean.",
                error_code="invalid_request",
            )
        existing = before.get(name) or []
        unchanged = same_tags(existing, desired)
        if unchanged and param in already_locked:
            continue
        if not unchanged:
            changes[name] = {"before": existing, "after": desired}
        lowered = {d.lower() for d in desired}
        tag_edits.append({
            "field": name,
            "param": param,
            "desired": desired,
            "remove": [e for e in existing if e.lower() not in lowered],
        })

    return before, changes, field_edits, tag_edits


def apply_metadata_plan(item, field_edits, tag_edits):
    """Issue the writes. Returns what was sent, for the caller to report.

    Removals go in their own request ahead of the set, so replacement is two
    observable steps rather than a thing Plex's edit endpoint may or may not
    mean by a repeated parameter.
    """
    sent = []
    if field_edits:
        item.edit(**field_edits)
        sent.append({
            "operation": "fields",
            "fields": sorted(k[:-len(".value")] for k in field_edits
                             if k.endswith(".value")),
        })
    for edit in tag_edits:
        if edit["remove"]:
            item.edit(**tag_params(edit["param"], edit["remove"], remove=True))
            sent.append({"operation": "remove", "field": edit["field"],
                         "values": edit["remove"]})
        if edit["desired"]:
            item.edit(**tag_params(edit["param"], edit["desired"]))
            sent.append({"operation": "set", "field": edit["field"],
                         "values": edit["desired"]})
    return sent


def verify_metadata(item, changes):
    """Read the item back and check every requested value actually landed.

    Plex returns 200 for edits it does not apply. Without this the tool would
    report a corrected year that is still wrong on the server, which is worse
    than reporting a failure because it stops anyone looking again.
    """
    try:
        item.reload()
    except Exception as exc:
        return False, [{"field": "*", "error": f"readback failed: {exc}"}], None
    after = metadata_snapshot(item)
    mismatches = []
    for field, change in changes.items():
        want, got = change["after"], after.get(field)
        if field in EDITABLE_TAGS:
            if not same_tags(want, got or []):
                mismatches.append({"field": field, "requested": want, "live": got})
        elif want != got:
            mismatches.append({"field": field, "requested": want, "live": got})
    return not mismatches, mismatches, after


def update_spec(args):
    """The subset of a call that describes what to write."""
    keys = list(EDITABLE_FIELDS) + list(EDITABLE_TAGS)
    keys += [f"clear_{name}" for name in EDITABLE_TAGS]
    return {k: args.get(k) for k in keys}


def tag_array(desc):
    return {"type": "array", "items": {"type": "string"}, "description": desc}


@tool(
    "Read one item's editable metadata exactly as Plex holds it - title, year, "
    "sort and original title, genres, labels, collections, countries, which "
    "fields are locked, and the files behind it. Do this before editing: the "
    "arrays in update_item_metadata replace what is there, so you need the "
    "current list to write a correct one.",
    {
        "rating_key": s("rating_key of the item, from any search result."),
        "query": s("Title, if you do not have a rating_key. Add the year - "
                   "'Black Sunday (1960)' - when the title alone is ambiguous."),
    },
)
def get_item_metadata(rating_key=None, query=None):
    item = resolve_editable_item(rating_key, query)
    out = {"ok": True}
    out.update(metadata_snapshot(item, include_raw=True))
    out["label"] = describe_item(item)["label"]
    return out


@tool(
    "Correct one item's metadata: fix a wrong title or year from a bad match, "
    "replace wrong genres, or apply a label. Arrays REPLACE - pass the full "
    "intended list, not just the addition. Omitted fields are untouched. "
    "Nothing is written unless confirm=true; without it you get the diff. "
    "Edited fields are locked so a later metadata refresh cannot undo the fix.",
    {
        "rating_key": s("rating_key of the item. Required - a write is never "
                        "made off a fuzzy title. Get one from get_item_metadata "
                        "or search."),
        "title": s("Replacement title."),
        "year": i("Replacement release year."),
        "original_title": s("Original-language title."),
        "sort_title": s("Title to sort under."),
        "summary": s("Replacement plot summary."),
        "genres": tag_array("Full intended genre list. Replaces the existing "
                            "genres outright, so include the ones to keep."),
        "labels": tag_array("Full intended label list, e.g. ['Horror Marathon']. "
                            "Replaces the existing labels."),
        "collections": tag_array("Full intended collection list. Replaces the "
                                 "existing collections."),
        "countries": tag_array("Full intended country list. Replaces the "
                               "existing countries."),
        "clear_genres": b("Erase every genre. Required to send an empty list."),
        "clear_labels": b("Erase every label. Required to send an empty list."),
        "clear_collections": b("Erase every collection."),
        "clear_countries": b("Erase every country."),
        "dry_run": b("Return the diff and write nothing, even if confirm is set.",
                     False),
        "confirm": b("Must be true for anything to be written.", False),
    },
    required=["rating_key"],
)
def update_item_metadata(rating_key=None, title=None, year=None,
                         original_title=None, sort_title=None, summary=None,
                         genres=None, labels=None, collections=None,
                         countries=None, clear_genres=False, clear_labels=False,
                         clear_collections=False, clear_countries=False,
                         dry_run=False, confirm=False):
    if rating_key in (None, ""):
        raise ToolError(
            "rating_key is required. Find it with get_item_metadata or search - "
            "a title alone is not enough to write against.",
            error_code="invalid_request",
        )
    item = resolve_editable_item(rating_key=rating_key)
    spec = update_spec(locals())
    before, changes, field_edits, tag_edits = plan_metadata_update(item, spec)
    label = describe_item(item)["label"]

    if not changes and not field_edits and not tag_edits:
        log(f"update_item_metadata rating_key={before['rating_key']} no-op")
        return {
            "ok": True, "rating_key": before["rating_key"], "item": label,
            "applied": False, "changes": {}, "before": before,
            "note": "The item already matches everything requested.",
        }

    if dry_run or not confirm:
        log(f"update_item_metadata rating_key={before['rating_key']} "
            f"fields={sorted(changes)} dry_run")
        return {
            "ok": True, "rating_key": before["rating_key"], "item": label,
            "applied": False, "dry_run": True, "changes": changes,
            "before": before,
            "note": ("dry_run was set, so nothing was written."
                     if dry_run else
                     "Nothing was written. Call again with confirm=true to "
                     "apply this diff."),
        }

    try:
        sent = apply_metadata_plan(item, field_edits, tag_edits)
    except Exception as exc:
        log(f"update_item_metadata rating_key={before['rating_key']} "
            f"fields={sorted(changes)} rejected")
        raise ToolError(
            f"Plex rejected the edit: {type(exc).__name__}: {exc}",
            error_code="plex_rejected",
            rating_key=before["rating_key"],
            attempted=changes,
        )
    invalidate_library_cache()
    verified, mismatches, after = verify_metadata(item, changes)
    log(f"update_item_metadata rating_key={before['rating_key']} "
        f"fields={sorted(changes)} applied verified={verified}")

    result = {
        "ok": bool(verified),
        "rating_key": before["rating_key"],
        "item": label,
        "applied": True,
        "verified": bool(verified),
        "changes": changes,
        "before": before,
        "after": after,
        "writes": sent,
    }
    if not verified:
        result["error_code"] = "readback_mismatch"
        result["error"] = (
            "Plex accepted the edit but the item does not read back with the "
            "requested values. The 'after' block is what is on the server now."
        )
        result["mismatches"] = mismatches
    return result


@tool(
    "Apply a reviewed set of metadata corrections in one pass, up to 25 items. "
    "Every target is resolved and validated before anything is written, so a "
    "bad entry costs the batch rather than leaving a half-corrected library. "
    "Each item is then written and read back on its own and reported on its "
    "own. Nothing is written unless confirm=true.",
    {
        "updates": {
            "type": "array",
            "description": (
                "One object per item, each with a rating_key plus the same "
                "fields update_item_metadata takes. Arrays replace."
            ),
            "items": {"type": "object"},
        },
        "dry_run": b("Return the full diff and write nothing.", False),
        "confirm": b("Must be true for anything to be written.", False),
    },
    required=["updates"],
)
def batch_update_item_metadata(updates=None, dry_run=False, confirm=False):
    if isinstance(updates, str):
        try:
            updates = json.loads(updates)
        except ValueError:
            raise ToolError("updates did not parse as JSON.",
                            error_code="invalid_request")
    if not isinstance(updates, (list, tuple)) or not updates:
        raise ToolError("updates must be a non-empty array of objects.",
                        error_code="invalid_request")
    if len(updates) > BATCH_LIMIT:
        raise ToolError(
            f"{len(updates)} updates is over the {BATCH_LIMIT}-item limit. "
            "Split the pass - a batch nobody read before confirming is the "
            "thing the limit is for.",
            error_code="invalid_request",
        )

    # Validate everything first. Half a tagging pass is harder to reason about
    # than none of one, because you cannot tell by looking which half ran.
    planned, problems, seen = [], [], {}
    for index, entry in enumerate(updates):
        if not isinstance(entry, dict):
            problems.append({"index": index, "error": "not an object",
                             "error_code": "invalid_request"})
            continue
        key = text(entry.get("rating_key")).strip()
        if not key:
            problems.append({"index": index, "error": "rating_key is required",
                             "error_code": "invalid_request"})
            continue
        if key in seen:
            problems.append({
                "index": index, "rating_key": key,
                "error": f"rating_key {key} also appears at index {seen[key]}; "
                         "merge them into one entry.",
                "error_code": "invalid_request",
            })
            continue
        seen[key] = index
        try:
            item = resolve_editable_item(rating_key=key)
            before, changes, field_edits, tag_edits = plan_metadata_update(
                item, update_spec(entry))
        except ToolError as exc:
            problem = {"index": index, "rating_key": key, "error": str(exc)}
            problem.update(exc.extra)
            problems.append(problem)
            continue
        except Exception as exc:
            problems.append({"index": index, "rating_key": key,
                             "error": f"{type(exc).__name__}: {exc}",
                             "error_code": "plex_rejected"})
            continue
        planned.append({
            "index": index, "item": item, "label": describe_item(item)["label"],
            "before": before, "changes": changes,
            "field_edits": field_edits, "tag_edits": tag_edits,
        })

    if problems:
        return {
            "ok": False,
            "error": f"{len(problems)} of {len(updates)} updates did not "
                     "validate; nothing was written.",
            "error_code": "invalid_request",
            "applied": False,
            "problems": problems,
            "would_change": [{"rating_key": p["before"]["rating_key"],
                              "item": p["label"], "changes": p["changes"]}
                             for p in planned],
        }

    if dry_run or not confirm:
        log(f"batch_update_item_metadata count={len(planned)} dry_run")
        return {
            "ok": True,
            "applied": False,
            "dry_run": True,
            "count": len(planned),
            "results": [{
                "rating_key": p["before"]["rating_key"], "item": p["label"],
                "changes": p["changes"], "before": p["before"],
            } for p in planned],
            "note": ("dry_run was set, so nothing was written."
                     if dry_run else
                     "Nothing was written. Call again with confirm=true to "
                     "apply this diff."),
        }

    results = []
    for plan in planned:
        row = {"rating_key": plan["before"]["rating_key"], "item": plan["label"],
               "changes": plan["changes"], "before": plan["before"]}
        if not plan["changes"] and not plan["field_edits"] and not plan["tag_edits"]:
            row.update({"ok": True, "verified": True, "applied": False,
                        "note": "Already matches everything requested."})
            results.append(row)
            continue
        try:
            row["writes"] = apply_metadata_plan(
                plan["item"], plan["field_edits"], plan["tag_edits"])
        except Exception as exc:
            # One rejection must not take the report for the others with it.
            row.update({"ok": False, "verified": False, "applied": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "error_code": "plex_rejected"})
            results.append(row)
            continue
        verified, mismatches, after = verify_metadata(plan["item"], plan["changes"])
        row.update({"ok": bool(verified), "verified": bool(verified),
                    "applied": True, "after": after})
        if not verified:
            row.update({"error_code": "readback_mismatch",
                        "error": "Written, but the item does not read back with "
                                 "the requested values.",
                        "mismatches": mismatches})
        results.append(row)

    invalidate_library_cache()
    failed = [r["rating_key"] for r in results if not r.get("ok")]
    log(f"batch_update_item_metadata count={len(results)} "
        f"verified={len(results) - len(failed)} failed={len(failed)}")
    return {
        "ok": not failed,
        "applied": True,
        "count": len(results),
        "verified_count": len(results) - len(failed),
        "failed": failed,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Artwork
#
# A movie with no poster is a black rectangle in every grid Plex draws, which
# is the most visible metadata failure there is and the one with no text to
# search for. Two ways to fix one, and the order matters:
#
#   setting a poster Plex's own agent already offers, which is what `posters()`
#   returns once an item is matched - free, correct, and the right answer
#   nearly every time;
#
#   uploading one from a URL, which makes the Plex server fetch whatever is at
#   the other end. That is an agent choosing a URL off the internet and a
#   server downloading it, so it is the escape hatch and not the default.
#
# If an item has no provider posters at all, that is not an artwork problem. It
# is an unmatched item, and fix_match below is the actual repair.
# ---------------------------------------------------------------------------

ARTWORK_LIMIT = 15


def strip_token(value):
    """plexapi's thumbUrl helpers embed the token. Nothing here returns one."""
    text_value = text(value)
    if "X-Plex-Token" not in text_value:
        return text_value or None
    return re.sub(r"[?&]X-Plex-Token=[^&]*", "", text_value) or None


def artwork_candidates(item, kind):
    """Posters or backgrounds the item's agent is offering, newest selection first."""
    try:
        found = item.posters() if kind == "poster" else item.arts()
    except Exception as exc:
        log(f"{kind} listing failed for {item.ratingKey}: {exc}")
        return [], f"{type(exc).__name__}: {exc}"
    out = []
    for entry in found[:ARTWORK_LIMIT]:
        out.append({
            "id": getattr(entry, "ratingKey", None),
            "provider": getattr(entry, "provider", None) or "agent",
            "selected": bool(getattr(entry, "selected", False)),
            "preview": strip_token(getattr(entry, "thumb", None)),
        })
    return out, None


# The three image slots an item has, and what Plex calls each one on the way
# in and on the way out. Plex is inconsistent enough here - the background is
# "art", the logo locks under "clearLogo", the poster is "thumb" - that three
# near-identical code paths is how one of them ends up locking the wrong field.
ARTWORK_SLOTS = {
    "poster": {"attr": "thumb", "lock": "thumb",
               "list": "posters", "upload": "uploadPoster"},
    "art": {"attr": "art", "lock": "art",
            "list": "arts", "upload": "uploadArt"},
    "logo": {"attr": "logo", "lock": "clearLogo",
             "list": "logos", "upload": "uploadLogo"},
}


def artwork_state(item):
    locked = set(locked_fields(item))
    out = {}
    for slot, spec in ARTWORK_SLOTS.items():
        value = getattr(item, spec["attr"], None)
        out[f"has_{slot}"] = bool(value)
        out[slot] = strip_token(value)
        out[f"{slot}_locked"] = spec["lock"] in locked
    return out


def check_art_url(url, field):
    """Refuse a URL the Plex server should not be made to fetch.

    An artwork URL is chosen by a model and downloaded by the media server, so
    which hosts can reach it is configuration rather than something the model
    argues its way into. The default list is the two art APIs this server knows
    how to query; PLEX_ART_HOSTS replaces it, and "*" turns the check off for
    anyone who would rather manage that themselves.
    """
    raw = text(url).strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ToolError(
            f"{field} must be an http(s) URL, not {parsed.scheme or 'a bare path'!r}.",
            error_code="invalid_request",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolError(f"{field} has no host.", error_code="invalid_request")
    if "*" in ART_HOSTS:
        return
    if not any(host == allowed or host.endswith("." + allowed)
               for allowed in ART_HOSTS):
        raise ToolError(
            f"{host} is not an allowed artwork host. The Plex server would "
            "have to download from it, so the list is configuration: set "
            "PLEX_ART_HOSTS to add one, or '*' to allow any.",
            error_code="invalid_request",
            allowed_hosts=list(ART_HOSTS),
        )


@tool(
    "List the posters and background art available for one item, plus what is "
    "set now. An item with no candidates at all is not missing artwork - it is "
    "unmatched, and list_match_candidates is the repair.",
    {
        "rating_key": s("rating_key of the item."),
        "query": s("Title, if you do not have a rating_key."),
    },
)
def get_artwork(rating_key=None, query=None):
    item = resolve_editable_item(rating_key, query)
    posters, poster_error = artwork_candidates(item, "poster")
    arts, art_error = artwork_candidates(item, "art")
    out = {"ok": True, "rating_key": str(item.ratingKey),
           "item": describe_item(item)["label"]}
    out.update(artwork_state(item))
    out["posters"] = posters
    out["arts"] = arts
    if poster_error:
        out["poster_listing_error"] = poster_error
    if art_error:
        out["art_listing_error"] = art_error
    if not posters and not out["has_poster"]:
        out["note"] = (
            "No poster set and no candidates offered. That is the signature of "
            "an unmatched item - run list_match_candidates rather than "
            "uploading a poster onto a record that is still wrong."
        )
    return out


@tool(
    "Set an item's poster, background art or clearlogo. Prefer the *_id form "
    "with an id from get_artwork or find_alternate_art - those are candidates "
    "Plex already holds. A URL makes the Plex server download from it, so only "
    "hosts on the PLEX_ART_HOSTS allowlist are accepted. Writes nothing "
    "without confirm=true, and reads the result back before reporting success.",
    {
        "rating_key": s("rating_key of the item."),
        "poster_id": s("id of a poster candidate."),
        "art_id": s("id of a background candidate."),
        "logo_id": s("id of a clearlogo candidate."),
        "poster_url": s("URL of a poster, from find_alternate_art. Must be on "
                        "the artwork host allowlist."),
        "art_url": s("URL of a background image."),
        "logo_url": s("URL of a clearlogo image."),
        "lock": b("Lock the artwork so a later refresh cannot replace it.", True),
        "dry_run": b("Report what would be set and change nothing.", False),
        "confirm": b("Must be true for anything to be set.", False),
    },
    required=["rating_key"],
)
def set_artwork(rating_key=None, poster_id=None, art_id=None, logo_id=None,
                poster_url=None, art_url=None, logo_url=None, lock=True,
                dry_run=False, confirm=False):
    if rating_key in (None, ""):
        raise ToolError("rating_key is required.", error_code="invalid_request")

    wanted = {}
    for slot in ARTWORK_SLOTS:
        chosen_id = {"poster": poster_id, "art": art_id, "logo": logo_id}[slot]
        chosen_url = {"poster": poster_url, "art": art_url, "logo": logo_url}[slot]
        if chosen_id and chosen_url:
            raise ToolError(f"Pass {slot}_id or {slot}_url, not both.",
                            error_code="invalid_request")
        if chosen_url:
            check_art_url(chosen_url, f"{slot}_url")
            wanted[slot] = {"url": chosen_url}
        elif chosen_id:
            wanted[slot] = {"id": str(chosen_id)}
    if not wanted:
        raise ToolError(
            "Nothing to set. Pass one of poster_id, poster_url, art_id, "
            "art_url, logo_id or logo_url.",
            error_code="invalid_request",
        )

    item = resolve_editable_item(rating_key=rating_key)
    before = artwork_state(item)
    label = describe_item(item)["label"]
    plan = {f"{slot}_{'url' if 'url' in how else 'id'}": list(how.values())[0]
            for slot, how in wanted.items()}

    if dry_run or not confirm:
        return {
            "ok": True, "rating_key": str(item.ratingKey), "item": label,
            "applied": False, "dry_run": True, "before": before, "plan": plan,
            "note": ("dry_run was set, so nothing was changed." if dry_run else
                     "Nothing was changed. Call again with confirm=true."),
        }

    def candidates(slot):
        lister = getattr(item, ARTWORK_SLOTS[slot]["list"], None)
        if lister is None:
            raise ToolError(f"This item has no {slot} candidates to choose from.",
                            error_code="not_found")
        return lister()

    # Selecting by id is a lookup against the live candidate list rather than a
    # bare PUT, so an id that has gone stale is a named error instead of a
    # silent no-op that still reports success.
    def select(slot, wanted_id):
        pool = candidates(slot)
        for entry in pool:
            if str(getattr(entry, "ratingKey", "")) == wanted_id:
                entry.select()
                return
        raise ToolError(
            f"No {slot} with id {wanted_id!r} is offered for this item. "
            "Re-read get_artwork - the candidate list changes when an item is "
            "rematched.",
            error_code="not_found",
            available=[str(getattr(e, "ratingKey", "")) for e in pool[:ARTWORK_LIMIT]],
        )

    try:
        for slot, how in wanted.items():
            if "id" in how:
                select(slot, how["id"])
            else:
                getattr(item, ARTWORK_SLOTS[slot]["upload"])(url=how["url"])
        if lock:
            item.edit(**{f"{ARTWORK_SLOTS[slot]['lock']}.locked": 1
                         for slot in wanted})
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Plex rejected the artwork change: "
                        f"{type(exc).__name__}: {exc}",
                        error_code="plex_rejected",
                        rating_key=str(item.ratingKey))

    invalidate_library_cache()
    try:
        item.reload()
    except Exception as exc:
        raise ToolError(f"Readback failed: {exc}",
                        error_code="readback_mismatch",
                        rating_key=str(item.ratingKey))
    after = artwork_state(item)

    mismatches = []
    for slot, how in wanted.items():
        if "id" in how:
            # The precise check: the candidate list should now mark that id
            # chosen. An upload has nothing to compare against, so it is only
            # ever "changed and non-empty", and the note below says so.
            try:
                chosen = [e for e in candidates(slot)
                          if getattr(e, "selected", False)
                          and str(getattr(e, "ratingKey", "")) == how["id"]]
            except ToolError:
                chosen = []
            if not chosen:
                mismatches.append({"field": slot, "requested": how["id"],
                                   "live": "not marked selected"})
        elif not after[f"has_{slot}"] or after[slot] == before[slot]:
            mismatches.append({"field": slot, "requested": how["url"],
                               "live": after[slot]})

    log(f"set_artwork rating_key={item.ratingKey} set={sorted(plan)} "
        f"verified={not mismatches}")
    result = {
        "ok": not mismatches, "rating_key": str(item.ratingKey), "item": label,
        "applied": True, "verified": not mismatches, "plan": plan,
        "before": before, "after": after,
    }
    if mismatches:
        result["error_code"] = "readback_mismatch"
        result["error"] = "The artwork change did not read back."
        result["mismatches"] = mismatches
    elif any("url" in how for how in wanted.values()):
        result["note"] = (
            "Uploaded artwork is verified only as 'changed and non-empty' - "
            "Plex stores it under its own key, so there is nothing to compare "
            "against the source URL. Look at it before calling it correct."
        )
    return result


# ---------------------------------------------------------------------------
# Alternate artwork
#
# Plex's own agent offers a handful of posters per film and picks one. There
# are usually dozens: every international release, every textless variant, and
# on fanart.tv a whole layer of community-made art that no metadata agent ships.
#
# This reads them. Two sources, both proper APIs with free keys, neither of
# them scraped:
#
#   TMDB /movie/{id}/images - every poster, backdrop and logo the database
#   holds, in every language, with the sizes and the vote counts.
#
#   fanart.tv - community-curated alternates. This is where the interesting
#   art is: textless posters, clearlogos, disc art, the stuff that exists
#   because somebody made it rather than because a studio shipped it.
#
# Scraping IMP Awards or MoviePosterDB would mean parsing HTML nobody promised
# to keep stable, against terms that do not permit it, and then hotlinking
# images off a server that pays to serve them. Both APIs below give the same
# posters, keyed and versioned, for free.
#
# Neither key is required. Without them this still reports what Plex already
# has, which on a matched film is more than most people realise.
# ---------------------------------------------------------------------------

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
FANART_API_KEY = os.environ.get("FANART_API_KEY", "")

# Hosts set_artwork will download from. A URL is chosen by a model and fetched
# by the media server, so the set of hosts that can reach it is configuration
# and not something the model gets to decide. "*" turns the check off.
ART_HOSTS = [h.strip().lower() for h in os.environ.get(
    "PLEX_ART_HOSTS", "image.tmdb.org,assets.fanart.tv").split(",") if h.strip()]

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"
ART_HTTP_TIMEOUT = 10

# Our name -> (TMDB images key, fanart.tv keys). fanart splits logos by
# resolution and keeps the old low-res set under a separate name, so both are
# read and the HD one sorts first.
ART_KINDS = {
    "poster": ("posters", ("movieposter",)),
    "background": ("backdrops", ("moviebackground",)),
    "logo": ("logos", ("hdmovielogo", "movielogo")),
}


def http_json(url, headers=None, timeout=ART_HTTP_TIMEOUT):
    """GET some JSON, or raise something with the service's name in it."""
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def external_ids(item):
    """The item's ids at TMDB, IMDB and TVDB, whichever agent matched it.

    The modern Plex agent stores plex:// as the primary guid and hangs the real
    external ids off <Guid> children; the legacy agents put one of them in the
    guid itself. Both shapes are read, because a library that has been running
    for years contains both and the one you cannot read is the one you need.
    """
    found = {}
    for entry in (getattr(item, "guids", None) or []):
        raw = text(getattr(entry, "id", ""))
        if "://" in raw:
            source, _, value = raw.partition("://")
            value = value.split("?")[0].strip()
            if value:
                found.setdefault(source.strip().lower(), value)
    legacy = text(getattr(item, "guid", ""))
    match = re.search(r"agents\.(imdb|themoviedb|thetvdb)://([^?/]+)", legacy)
    if match:
        source = {"themoviedb": "tmdb", "thetvdb": "tvdb"}.get(match.group(1),
                                                               match.group(1))
        found.setdefault(source, match.group(2))
    return found


def tmdb_headers_and_key(base_url):
    """TMDB takes a v3 key in the query or a v4 token in a header."""
    if TMDB_API_KEY.startswith("ey"):  # v4 tokens are JWTs
        return base_url, {"Authorization": f"Bearer {TMDB_API_KEY}"}
    joiner = "&" if "?" in base_url else "?"
    return f"{base_url}{joiner}api_key={TMDB_API_KEY}", {}


def tmdb_movie_id(ids):
    """A TMDB id, looked up from IMDB if that is all the item carries."""
    if ids.get("tmdb"):
        return ids["tmdb"], None
    if not ids.get("imdb"):
        return None, "the item has neither a TMDB nor an IMDB id"
    url, headers = tmdb_headers_and_key(
        f"https://api.themoviedb.org/3/find/{ids['imdb']}"
        "?external_source=imdb_id")
    try:
        results = http_json(url, headers).get("movie_results") or []
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not results:
        return None, f"TMDB knows no movie for {ids['imdb']}"
    return str(results[0].get("id")), None


def tmdb_art(ids, kind):
    if not TMDB_API_KEY:
        return [], "TMDB_API_KEY is not set"
    movie_id, problem = tmdb_movie_id(ids)
    if not movie_id:
        return [], problem
    # include_image_language=null keeps the textless variants, which are the
    # ones worth having and the ones a language filter would otherwise drop.
    url, headers = tmdb_headers_and_key(
        f"https://api.themoviedb.org/3/movie/{movie_id}/images"
        "?include_image_language=en,null")
    try:
        payload = http_json(url, headers)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"

    out = []
    for entry in (payload.get(ART_KINDS[kind][0]) or []):
        path = entry.get("file_path")
        if not path:
            continue
        out.append({
            "source": "tmdb",
            "kind": kind,
            "url": TMDB_IMAGE_BASE + path,
            "language": entry.get("iso_639_1") or "textless",
            "size": f"{entry.get('width')}x{entry.get('height')}",
            "score": round(float(entry.get("vote_average") or 0), 2),
            "votes": entry.get("vote_count"),
        })
    out.sort(key=lambda d: (-(d["score"] or 0), -(d["votes"] or 0)))
    return out, None


def fanart_art(ids, kind):
    if not FANART_API_KEY:
        return [], "FANART_API_KEY is not set"
    key = ids.get("tmdb") or ids.get("imdb")
    if not key:
        return [], "the item has neither a TMDB nor an IMDB id"
    try:
        payload = http_json(
            f"https://webservice.fanart.tv/v3/movies/{key}"
            f"?api_key={urllib.parse.quote(FANART_API_KEY)}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return [], "fanart.tv has nothing for this film"
        return [], f"HTTP {exc.code}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"

    out = []
    for rank, group in enumerate(ART_KINDS[kind][1]):
        for entry in (payload.get(group) or []):
            url = entry.get("url")
            if not url:
                continue
            language = text(entry.get("lang")).strip()
            out.append({
                "source": "fanart",
                "kind": kind,
                "url": url,
                "language": "textless" if language in ("", "00") else language,
                "likes": int(entry.get("likes") or 0),
                "variant": group,
                "_rank": rank,
            })
    out.sort(key=lambda d: (d.pop("_rank"), -d["likes"]))
    return out, None


def plex_art(item, kind):
    """What the item's own agent already offers. Free, and usually ignored."""
    try:
        if kind == "poster":
            found = item.posters()
        elif kind == "background":
            found = item.arts()
        elif hasattr(item, "logos"):
            found = item.logos()
        else:
            return [], "this plexapi does not expose logos"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return [{
        "source": "plex",
        "kind": kind,
        "id": getattr(entry, "ratingKey", None),
        "provider": getattr(entry, "provider", None) or "agent",
        "selected": bool(getattr(entry, "selected", False)),
        "preview": strip_token(getattr(entry, "thumb", None)),
    } for entry in found], None


@tool(
    "Find alternate artwork for one item - every poster, background or "
    "clearlogo available, not just the one Plex picked. Reads three sources: "
    "the item's own agent, TMDB (every language plus textless variants) and "
    "fanart.tv (community-made alternates). Returns candidates and changes "
    "nothing; apply one with set_artwork, using poster_id for a Plex candidate "
    "and poster_url for an external one.",
    {
        "rating_key": s("rating_key of the item."),
        "query": s("Title, if you do not have a rating_key."),
        "kind": s("poster, background or logo. Default poster.", "poster"),
        "language": s("Two-letter code, or 'textless' for art with no title on "
                      "it. Default: everything."),
        "source": s("plex, tmdb, fanart or all. Default all.", "all"),
        "limit": i("Candidates per source. Default 10.", 10),
    },
)
def find_alternate_art(rating_key=None, query=None, kind="poster",
                       language=None, source="all", limit=10):
    kind = text(kind, "poster").strip().lower()
    if kind in ("art", "background", "backdrop", "fanart"):
        kind = "background"
    if kind not in ART_KINDS:
        raise ToolError(f"Unknown kind {kind!r}.", error_code="invalid_request",
                        valid_kinds=list(ART_KINDS))
    source = text(source, "all").strip().lower()
    valid_sources = ("all", "plex", "tmdb", "fanart")
    if source not in valid_sources:
        raise ToolError(f"Unknown source {source!r}.",
                        error_code="invalid_request", valid_sources=list(valid_sources))
    limit = max(1, int(limit or 10))
    want_language = text(language).strip().lower() or None

    item = resolve_editable_item(rating_key, query)
    ids = external_ids(item)
    candidates, unavailable = [], {}

    for name, fetch in (("plex", lambda: plex_art(item, kind)),
                        ("tmdb", lambda: tmdb_art(ids, kind)),
                        ("fanart", lambda: fanart_art(ids, kind))):
        if source not in ("all", name):
            continue
        found, problem = fetch()
        if problem:
            unavailable[name] = problem
            log(f"find_alternate_art {name} unavailable: {problem}")
        if want_language:
            found = [c for c in found
                     if c.get("language", "").lower() == want_language]
        candidates.extend(found[:limit])

    out = {
        "ok": True,
        "rating_key": str(item.ratingKey),
        "item": describe_item(item)["label"],
        "kind": kind,
        "external_ids": ids,
        "candidates": candidates,
        "count": len(candidates),
        "current": artwork_state(item),
    }
    if unavailable:
        out["unavailable"] = unavailable
    if not ids:
        out["note"] = (
            "This item carries no external id, which means it is unmatched - "
            "so TMDB and fanart.tv cannot be asked about it. Run fix_match "
            "first and the artwork usually arrives on its own."
        )
    elif not candidates:
        out["note"] = (
            "No candidates from any source that answered. Check 'unavailable' "
            "before concluding the art does not exist."
        )
    else:
        out["note"] = (
            "Apply a plex candidate with set_artwork poster_id=<id>, and a "
            "tmdb or fanart one with poster_url=<url>. 'textless' art carries "
            "no title, which is what you want when Plex draws the title itself."
        )
    return out


# ---------------------------------------------------------------------------
# Matching
#
# The repair that update_item_metadata is not. An item whose title is
# "The Entity Horror" with no poster is usually not a record with three wrong
# fields - it is a file Plex never matched to anything, so editing the title
# and adding a genre leaves a correctly labelled record that still has no
# summary, no cast, no ratings and no artwork.
#
# fix_match re-binds the file to the right provider entry and every one of
# those arrives at once. So the order for a broken item is: match it, see what
# landed, correct the residue, lock that. Not the other way round - locked
# fields are exactly what a rematch cannot overwrite, which is why unlocking is
# a tool of its own and why fix_match will tell you when locks are in its way.
# ---------------------------------------------------------------------------

MATCH_WAIT_SECONDS = 12


def is_unmatched(item):
    guid = text(getattr(item, "guid", "")).strip()
    return not guid or guid.startswith("local://") or "agents.none" in guid


@tool(
    "Ask Plex's metadata agent what this file might actually be. Returns "
    "candidate matches with name, year and score - it does not change "
    "anything. Use it on an item whose title looks like a filename, or that "
    "has no poster and no artwork candidates. Pass title/year to search for "
    "something other than what the item currently claims to be.",
    {
        "rating_key": s("rating_key of the item."),
        "query": s("Title, if you do not have a rating_key."),
        "title": s("Search under this title instead of the item's current one. "
                   "This is the one that matters for a mangled title."),
        "year": i("Search under this year instead of the item's current one."),
        "agent": s("Metadata agent to ask, e.g. themoviedb, imdb, thetvdb. "
                   "Defaults to the library's own agent."),
    },
)
def list_match_candidates(rating_key=None, query=None, title=None, year=None,
                          agent=None):
    item = resolve_editable_item(rating_key, query)
    kwargs = {}
    if title is not None:
        kwargs["title"] = text(title).strip()
    if year is not None:
        kwargs["year"] = int(year)
    if agent:
        kwargs["agent"] = text(agent).strip()
    try:
        found = item.matches(**kwargs) if kwargs else item.matches()
    except Exception as exc:
        raise ToolError(f"Plex could not search for matches: "
                        f"{type(exc).__name__}: {exc}",
                        error_code="plex_rejected",
                        rating_key=str(item.ratingKey))

    candidates = [{
        "guid": getattr(m, "guid", None),
        "name": getattr(m, "name", None),
        "year": getattr(m, "year", None),
        "score": getattr(m, "score", None),
    } for m in found[:10] if getattr(m, "guid", None)]

    current = metadata_snapshot(item)
    out = {
        "ok": True,
        "rating_key": str(item.ratingKey),
        "item": describe_item(item)["label"],
        "currently_unmatched": is_unmatched(item),
        "current_guid": getattr(item, "guid", None),
        "current": {k: current[k] for k in ("title", "year", "genres", "summary")},
        "files": [os.path.basename(p["file"] or "") for p in media_parts(item)],
        "locked_fields": current["locked_fields"],
        "candidates": candidates,
    }
    if not candidates:
        out["note"] = (
            "No candidates. The agent found nothing under that title - try "
            "list_match_candidates again with an explicit title= and year= for "
            "what you believe the film actually is."
        )
    if current["locked_fields"]:
        out["warning"] = (
            "Locked fields will survive a rematch unchanged: "
            f"{', '.join(current['locked_fields'])}. Unlock them with "
            "unlock_metadata_fields first if the rematch is meant to replace "
            "them."
        )
    return out


@tool(
    "Re-bind an item to the provider entry you picked from "
    "list_match_candidates. This replaces title, year, summary, genres, cast "
    "and artwork in one operation, which is the right repair for an unmatched "
    "or wrongly matched file - and a destructive one for a record someone "
    "already corrected by hand. Writes nothing without confirm=true.",
    {
        "rating_key": s("rating_key of the item."),
        "guid": s("guid of the chosen candidate from list_match_candidates."),
        "unlock_first": b("Unlock every locked field so the new match can "
                          "replace it. Without this, locked fields keep their "
                          "current values and the rematch looks like it half "
                          "worked.", False),
        "dry_run": b("Report what would happen and change nothing.", False),
        "confirm": b("Must be true for the match to be applied.", False),
    },
    required=["rating_key", "guid"],
)
def fix_match(rating_key=None, guid=None, unlock_first=False, dry_run=False,
              confirm=False):
    if rating_key in (None, "") or not text(guid).strip():
        raise ToolError("rating_key and guid are both required.",
                        error_code="invalid_request")
    item = resolve_editable_item(rating_key=rating_key)
    before = metadata_snapshot(item)
    before_art = artwork_state(item)
    before_guid = text(getattr(item, "guid", ""))
    label = describe_item(item)["label"]
    wanted = text(guid).strip()

    # Resolve the guid against a live candidate list rather than trusting it.
    # A guid invented or carried over from another item would otherwise be a
    # PUT that Plex accepts and quietly does nothing with.
    try:
        found = item.matches()
        chosen = next((m for m in found
                       if text(getattr(m, "guid", "")) == wanted), None)
        if chosen is None and before.get("title"):
            found = item.matches(title=before["title"], year=before.get("year"))
            chosen = next((m for m in found
                           if text(getattr(m, "guid", "")) == wanted), None)
    except Exception as exc:
        raise ToolError(f"Plex could not search for matches: "
                        f"{type(exc).__name__}: {exc}",
                        error_code="plex_rejected")
    if chosen is None:
        raise ToolError(
            f"guid {wanted!r} is not among the candidates Plex offers for this "
            "item. Re-run list_match_candidates - candidates depend on the "
            "title and year you searched under.",
            error_code="not_found",
            rating_key=str(item.ratingKey),
            available=[{"guid": getattr(m, "guid", None),
                        "name": getattr(m, "name", None),
                        "year": getattr(m, "year", None)} for m in found[:10]],
        )

    target = {"guid": wanted, "name": getattr(chosen, "name", None),
              "year": getattr(chosen, "year", None),
              "score": getattr(chosen, "score", None)}

    if dry_run or not confirm:
        out = {
            "ok": True, "rating_key": str(item.ratingKey), "item": label,
            "applied": False, "dry_run": True, "before": before,
            "before_artwork": before_art, "match": target,
            "note": ("dry_run was set, so nothing was changed." if dry_run else
                     "Nothing was changed. Call again with confirm=true."),
        }
        if before["locked_fields"] and not unlock_first:
            out["warning"] = (
                "These fields are locked and will NOT be replaced by the "
                f"match: {', '.join(before['locked_fields'])}. Pass "
                "unlock_first=true if the match is meant to own them."
            )
        return out

    try:
        if unlock_first and before["locked_fields"]:
            item.edit(**{f"{name}.locked": 0 for name in before["locked_fields"]})
        item.fixMatch(searchResult=chosen)
    except Exception as exc:
        raise ToolError(f"Plex rejected the match: {type(exc).__name__}: {exc}",
                        error_code="plex_rejected",
                        rating_key=str(item.ratingKey), attempted=target)

    # Plex applies a match asynchronously. Reporting the pre-match record as
    # the result is the obvious way to get this wrong, so wait for the guid to
    # actually turn over before reading anything else.
    settled, after = False, before
    deadline = time.time() + MATCH_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(1)
        try:
            item.reload()
        except Exception:
            continue
        if text(getattr(item, "guid", "")) != before_guid and not is_unmatched(item):
            settled = True
            break
    try:
        item.reload()
    except Exception:
        pass
    after = metadata_snapshot(item)
    after_art = artwork_state(item)
    invalidate_library_cache()

    log(f"fix_match rating_key={item.ratingKey} guid={wanted} "
        f"settled={settled}")
    result = {
        "ok": settled,
        "rating_key": str(item.ratingKey),
        "item": label,
        "applied": True,
        "verified": settled,
        "match": target,
        "before": before,
        "after": after,
        "before_artwork": before_art,
        "after_artwork": after_art,
        "guid_before": before_guid,
        "guid_after": getattr(item, "guid", None),
    }
    if not settled:
        result["error_code"] = "readback_mismatch"
        result["error"] = (
            f"The match was accepted but the item still reads as "
            f"{before_guid or 'unmatched'} after {MATCH_WAIT_SECONDS}s. Plex "
            "may still be working; re-read with get_item_metadata before "
            "retrying, and do not apply the match twice."
        )
    elif not after_art["has_poster"]:
        result["note"] = (
            "Matched, but still no poster. Check get_artwork - the agent may "
            "offer candidates that were not auto-selected."
        )
    return result


@tool(
    "Unlock metadata fields so a refresh or a rematch can replace them. The "
    "counterpart to the automatic locking update_item_metadata does - use it "
    "when a correction turns out to be wrong and the provider should own the "
    "field again.",
    {
        "rating_key": s("rating_key of the item."),
        "fields": {"type": "array", "items": {"type": "string"},
                   "description": "Plex field names to unlock, as reported in "
                                  "locked_fields, e.g. ['title','genre']. "
                                  "Omit to unlock every locked field."},
        "dry_run": b("Report what would be unlocked and change nothing.", False),
        "confirm": b("Must be true for anything to be unlocked.", False),
    },
    required=["rating_key"],
)
def unlock_metadata_fields(rating_key=None, fields=None, dry_run=False,
                           confirm=False):
    if rating_key in (None, ""):
        raise ToolError("rating_key is required.", error_code="invalid_request")
    item = resolve_editable_item(rating_key=rating_key)
    locked = locked_fields(item)
    wanted = normalize_tag_list(fields, "fields") if fields is not None else list(locked)
    unknown = [f for f in wanted if f not in locked]
    target = [f for f in wanted if f in locked]
    label = describe_item(item)["label"]

    if not target:
        return {"ok": True, "rating_key": str(item.ratingKey), "item": label,
                "applied": False, "locked_fields": locked,
                "note": ("Nothing to unlock." if not unknown else
                         f"None of {unknown} are locked on this item."),
                "not_locked": unknown}

    if dry_run or not confirm:
        return {"ok": True, "rating_key": str(item.ratingKey), "item": label,
                "applied": False, "dry_run": True, "would_unlock": target,
                "locked_fields": locked, "not_locked": unknown,
                "note": ("dry_run was set, so nothing was changed." if dry_run
                         else "Nothing was changed. Call again with confirm=true.")}

    try:
        item.edit(**{f"{name}.locked": 0 for name in target})
    except Exception as exc:
        raise ToolError(f"Plex rejected the unlock: {type(exc).__name__}: {exc}",
                        error_code="plex_rejected")
    invalidate_library_cache()
    try:
        item.reload()
    except Exception:
        pass
    still = locked_fields(item)
    remaining = [f for f in target if f in still]
    log(f"unlock_metadata_fields rating_key={item.ratingKey} "
        f"fields={target} verified={not remaining}")
    out = {"ok": not remaining, "rating_key": str(item.ratingKey), "item": label,
           "applied": True, "verified": not remaining, "unlocked": target,
           "locked_fields": still, "not_locked": unknown}
    if remaining:
        out["error_code"] = "readback_mismatch"
        out["error"] = f"Still locked after the write: {remaining}"
    return out


# ---------------------------------------------------------------------------
# Auditing
#
# find_gaps answers "what is obviously absent" - no year, no genres, no
# summary, a title full of release tags. It was built for files nobody matched
# and it finds those. It does not find the case this section exists for: an
# item that has a year and a summary and a plausible-looking title, and is
# still wrong.
#
# "The Entity Horror (1982)" trips none of find_gaps' checks. Neither does
# "L'occhio Che Uccide Peeping Tom" or "I 13 Spettri Thir13en Ghosts". They are
# all the same underlying failure - a filename that was never matched, so its
# title is whatever the file was called - but the tell is not release-group
# debris. It is a foreign release title welded to the English one, a stripped
# apostrophe, a genre word on the end, leetspeak from a stylised poster.
#
# Every check here reports a reason and a confidence and nothing else. None of
# them decides what a film is; that judgement needs to know what films exist,
# which is the agent's job and not this server's. What this does is turn 500
# items into the 30 worth looking at, with the filename attached, because the
# filename is usually the only honest identifier a broken item still has.
# ---------------------------------------------------------------------------

# Release-group debris. Only ever in a title Plex took from a filename.
FILENAME_DEBRIS = re.compile(
    r"\b(2160p|1080p|720p|480p|x264|x265|h ?264|h ?265|hevc|bluray|blu ray|"
    r"brrip|bdrip|webrip|web dl|hdtv|dvdrip|xvid|divx|aac|ac3|dts|remux|"
    r"proper|repack|extended cut|uncut|unrated)\b", re.I)

# "a k a" survives the punctuation folding that turns "a.k.a." into it.
ALIAS_MARKER = re.compile(r"\b(a k a|aka|alias)\b", re.I)

# A bare genre on the end is a filing convention, never a title: nobody
# released a film called "The Entity Horror".
GENRE_SUFFIX = re.compile(
    r"\b(horror|thriller|comedy|drama|action|western|sci fi|scifi|fantasy|"
    r"mystery|romance|documentary|animation|crime|war|musical|noir)$", re.I)

# A foreign article that kept its apostrophe or lost it to filename sanitising:
# "L'occhio" and "L Ultimo" are the same tell, so both fold to "l occhio". Two
# details are load-bearing: the leading boundary, or this matches the "l h"
# inside "Angel Heart", and the two-letter minimum on what follows, or it
# matches the "l a" in "L.A. Confidential".
STRIPPED_APOSTROPHE = re.compile(
    r"(?:^|\s)(l|d|dell|nell|all|sull|dall)\s+[a-z]{2,}", re.I)

# A digit inside a word - "Thir13en", "Se7en" - is poster styling that a
# filename kept and a metadata agent would not. Weak on its own: Se7en is a
# real title, so this only counts alongside something else.
LEETSPEAK = re.compile(r"[a-z]\d+[a-z]", re.I)

# Function words that are not also English words. Two or more of these next to
# English is a bilingual smash-up. Single letters and anything that collides
# with English ("a", "an", "i", "die", "con", "am", "as") are deliberately
# absent - a false positive here costs a human a read of something that was
# fine, and there is no shortage of real titles in other languages.
FOREIGN_FUNCTION_WORDS = frozenset("""
    il lo la gli della delle degli del dei di da dal dalla al alla allo ai agli
    sul sulla col nel nella nei che una uno questo questa sono per non tra fra
    el los las unos unas por que
    le les une des du dans pour avec qui au aux chez sans sous
    der das den dem eine einen und mit von fur zum zur auf aus teufels
    um uma dos nao
""".split())


def fold_title(title):
    """Lowercase, accent-free, punctuation-as-space.

    Apostrophes become spaces rather than vanishing, so "L'occhio Che Uccide"
    and "L Ultimo Esorcismo" present the stranded article the same way. Folding
    the apostrophe away instead - which is what normalize_spoken does, and what
    it should do for a room name - hides the single clearest sign that a title
    came out of a filename.
    """
    folded = unicodedata.normalize("NFKD", text(title))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def suspect_title(title):
    """Why this title looks like a filename rather than a film. Reasons only.

    Returns (reasons, confidence). Nothing here identifies the film - that
    needs knowing what films exist, which is the agent's job. This narrows a
    library to the titles worth a human read and says why each earned the look.

    Reasons come in two weights. A strong one stands alone; a weak one needs
    company, because every weak signal here has a real film behind it - Se7en
    has a digit inside a word, "Dr. Strangelove or: How I Learned to Stop
    Worrying and Love the Bomb" is thirteen words, and flagging either teaches
    an agent to distrust the whole sweep.

    Deliberately absent: comparing the title to the filename. A correctly
    matched film very often sits in a file named after it, so agreement there
    means nothing at all.
    """
    raw = text(title).strip()
    if not raw:
        return ["no title at all"], "high"
    folded = fold_title(raw)
    strong, weak = [], []

    if FILENAME_DEBRIS.search(folded):
        strong.append("carries release tags a real title never has")
    if ALIAS_MARKER.search(folded):
        strong.append("carries an alias marker - two titles welded together")
    if STRIPPED_APOSTROPHE.search(folded):
        strong.append("a stranded foreign article, apostrophe folded away by a "
                      "filename")
    if GENRE_SUFFIX.search(folded):
        strong.append("ends in a bare genre name, which is a filing convention "
                      "rather than a title")

    # Two of these is a bilingual smash-up. One is "La Dolce Vita" - a real
    # title in one language - so it only counts alongside something else.
    foreign = [w for w in folded.split() if w in FOREIGN_FUNCTION_WORDS]
    if len(foreign) >= 2:
        strong.append(f"foreign function words ({', '.join(foreign[:4])}) "
                      "alongside English - a release title and its translation "
                      "in one string")
    elif foreign:
        weak.append(f"a foreign function word ({foreign[0]})")

    if LEETSPEAK.search(raw):
        weak.append("a digit inside a word - poster styling a filename kept")
    numerals = re.findall(r"\d+", folded)
    if len(numerals) > len(set(numerals)):
        weak.append("the same number twice, which is how a title and its "
                    "translation read when both are present")
    words = folded.split()
    if len(words) >= 8:
        weak.append(f"{len(words)} words, long enough to be two titles")

    if strong:
        return strong + weak, "high"
    if len(weak) >= 2:
        return weak, "medium"
    return [], None


AUDIT_CHECKS = ("artwork", "title", "match", "fields", "labels")
AUDIT_LIMIT = 100


@tool(
    "Sweep a library for items that need fixing and report why, without "
    "changing anything. Finds what find_gaps cannot: missing posters, titles "
    "that are really filenames (a foreign title welded to the English one, a "
    "stripped apostrophe, a genre word on the end), unmatched files, and items "
    "missing the label a filter depends on. Returns the filename with each "
    "finding, because on a broken item that is the only honest identifier "
    "left. Identifying the actual film is your job, not this tool's.",
    {
        "library": s("Restrict to one library. Default: every library."),
        "checks": {"type": "array", "items": {"type": "string"},
                   "description": "Any of artwork, title, match, fields, "
                                  "labels. Default: all but labels."},
        "require_label": s("Label that should be present, e.g. 'Horror "
                           "Marathon'. Enables the labels check."),
        "when_genre": s("Only require that label on items carrying this genre, "
                        "e.g. 'Horror'. Without it the label is required on "
                        "everything in scope."),
        "limit": i("Findings to return. Default 100.", 100),
        "offset": i("Skip this many findings, for paging a long sweep.", 0),
    },
)
def audit_library(library=None, checks=None, require_label=None,
                  when_genre=None, limit=100, offset=0):
    wanted = [c.lower() for c in normalize_tag_list(checks, "checks")] \
        if checks is not None else ["artwork", "title", "match", "fields"]
    if require_label and "labels" not in wanted:
        wanted.append("labels")
    unknown = [c for c in wanted if c not in AUDIT_CHECKS]
    if unknown:
        raise ToolError(f"Unknown checks {unknown}.",
                        error_code="invalid_request", valid_checks=list(AUDIT_CHECKS))
    if "labels" in wanted and not require_label:
        raise ToolError("The labels check needs require_label.",
                        error_code="invalid_request")

    limit = max(1, int(limit or AUDIT_LIMIT))
    offset = max(0, int(offset or 0))
    want_label = text(require_label).strip().lower()
    want_genre = text(when_genre).strip().lower()

    findings, scanned, degraded_total = [], 0, 0
    for section in resolve_sections(library):
        if section.type not in ("movie", "show"):
            continue
        items, degraded = section_items(section, enriched=True)
        degraded_total += degraded
        for item in items:
            scanned += 1
            problems, confidence = [], None
            genres = [g.tag for g in (getattr(item, "genres", None) or [])]
            labels = [l.tag for l in (getattr(item, "labels", None) or [])]
            parts = media_parts(item)
            filename = os.path.basename(parts[0]["file"] or "") if parts else None

            if "artwork" in wanted:
                if not getattr(item, "thumb", None):
                    problems.append("no poster")
                if not getattr(item, "art", None):
                    problems.append("no background art")
            if "match" in wanted and is_unmatched(item):
                problems.append("never matched to a provider entry")
                confidence = "high"
            if "title" in wanted:
                reasons, level = suspect_title(getattr(item, "title", ""))
                problems.extend(reasons)
                if level == "high" or confidence == "high":
                    confidence = "high"
                elif level:
                    confidence = confidence or level
            if "fields" in wanted:
                if not getattr(item, "year", None):
                    problems.append("no year")
                if not genres:
                    problems.append("no genres")
                if not getattr(item, "summary", None):
                    problems.append("no summary")
            if "labels" in wanted:
                in_scope = not want_genre or want_genre in [g.lower() for g in genres]
                if in_scope and want_label not in [l.lower() for l in labels]:
                    problems.append(f"missing the {require_label!r} label")

            if problems:
                findings.append({
                    "rating_key": str(item.ratingKey),
                    "title": getattr(item, "title", None),
                    "year": getattr(item, "year", None),
                    "library": section.title,
                    "file": filename,
                    "genres": genres,
                    "labels": labels,
                    "has_poster": bool(getattr(item, "thumb", None)),
                    "guid": getattr(item, "guid", None),
                    "problems": problems,
                    "confidence": confidence or "low",
                })

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["confidence"], 3),
                                 -len(f["problems"]), f["title"] or ""))
    counts = Counter(p for f in findings for p in f["problems"])
    page = findings[offset:offset + limit]

    out = {
        "ok": True,
        "checks": wanted,
        "scanned": scanned,
        "finding_count": len(findings),
        "by_problem": dict(counts.most_common()),
        "by_confidence": dict(Counter(f["confidence"] for f in findings)),
        "findings": page,
        "returned": len(page),
        "hint": (
            "High confidence usually means unmatched: run list_match_candidates "
            "and fix_match, which restores title, year, summary, genres and "
            "artwork together. Save update_item_metadata for what a rematch "
            "cannot know - a deliberate label, a genre the provider gets wrong."
        ),
    }
    if offset + limit < len(findings):
        out["next_offset"] = offset + limit
    if degraded_total:
        out["degraded"] = (
            f"{degraded_total} items fell back to truncated listing data; their "
            "genres and labels may be incomplete."
        )
    return out


# ---------------------------------------------------------------------------
# Review documents
#
# Identifying "The Entity Horror" as The Entity (1982) is a judgement, and
# judgements at library scale want a human read before they are committed. This
# renders the sweep and whatever the agent proposes into one markdown file, so
# the pass is: audit, propose, write, someone reads it, confirmed batch.
#
# Writes land under PLEX_REVIEW_DIR and nowhere else. A media server has no
# business taking an arbitrary path from a model.
# ---------------------------------------------------------------------------


def review_dir():
    path = os.environ.get("PLEX_REVIEW_DIR") or os.path.join(
        os.path.expanduser("~"), "plex-reviews")
    os.makedirs(path, exist_ok=True)
    return path


def safe_review_name(name):
    base = os.path.basename(text(name).strip() or "review")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.") or "review"
    if not base.lower().endswith(".md"):
        base += ".md"
    return base


def render_row(entry):
    """One proposal as a markdown block: what is there now, what is proposed."""
    lines = []
    title = entry.get("title") or entry.get("rating_key") or "(untitled)"
    year = entry.get("year")
    lines.append(f"### {title}{f' ({year})' if year else ''}")
    lines.append("")
    lines.append(f"- **rating_key** `{entry.get('rating_key', '?')}`")
    if entry.get("file"):
        lines.append(f"- **file** `{entry['file']}`")
    if entry.get("problems"):
        lines.append("- **detected**")
        for problem in entry["problems"]:
            lines.append(f"  - {problem}")
    if entry.get("confidence"):
        lines.append(f"- **confidence** {entry['confidence']}")
    proposal = entry.get("proposed") or {}
    if proposal:
        lines.append("- **proposed**")
        for field in sorted(proposal):
            value = proposal[field]
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value) or "(cleared)"
            lines.append(f"  - `{field}` → {value}")
    if entry.get("note"):
        lines.append(f"- **note** {entry['note']}")
    lines.append("")
    return lines


@tool(
    "Write an audit and its proposed corrections to a markdown file for a "
    "human to read before anything is applied. Takes the findings from "
    "audit_library plus whatever you propose for each one. The file lands in "
    "PLEX_REVIEW_DIR; you cannot write anywhere else.",
    {
        "filename": s("File to write, e.g. 'horror-pass.md'. A bare name - "
                      "directories are not accepted."),
        "title": s("Heading for the document."),
        "summary": s("A paragraph on what this pass covered and what you "
                     "concluded. Written above the items."),
        "entries": {
            "type": "array",
            "description": (
                "One object per item: rating_key, title, year, file, problems "
                "(array), confidence, proposed (object of field -> new value), "
                "note. Anything missing is simply left out of the document."
            ),
            "items": {"type": "object"},
        },
        "apply_payload": {
            "type": "object",
            "description": (
                "Optional. The exact batch_update_item_metadata arguments this "
                "document is asking approval for, printed at the bottom so the "
                "reviewer can see what would actually run."
            ),
        },
    },
    required=["filename", "entries"],
)
def write_review_document(filename=None, title=None, summary=None, entries=None,
                          apply_payload=None):
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except ValueError:
            raise ToolError("entries did not parse as JSON.",
                            error_code="invalid_request")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ToolError("entries must be a non-empty array of objects.",
                        error_code="invalid_request")
    bad = [n for n, e in enumerate(entries) if not isinstance(e, dict)]
    if bad:
        raise ToolError(f"entries at {bad} are not objects.",
                        error_code="invalid_request")

    name = safe_review_name(filename)
    heading = text(title).strip() or "Plex metadata review"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    counted_word = "item" if len(entries) == 1 else "items"
    lines = [f"# {heading}", "",
             f"*Generated {stamp} — {len(entries)} {counted_word}*", ""]
    if summary:
        lines += [text(summary).strip(), ""]

    counted = Counter(p for e in entries for p in (e.get("problems") or []))
    if counted:
        lines += ["## What was detected", "", "| Problem | Items |",
                  "| --- | --- |"]
        lines += [f"| {problem} | {n} |" for problem, n in counted.most_common()]
        lines.append("")

    lines += ["## Nothing here has been applied", "",
              "Every item below is a proposal. Read them, then confirm the "
              "ones you want. Arrays replace what is on the item, so a genre "
              "list is the complete intended list.", "", "## Items", ""]
    for entry in entries:
        lines += render_row(entry)

    if apply_payload:
        lines += ["## To apply", "",
                  "`batch_update_item_metadata` with `confirm: true` and:", "",
                  "```json", json.dumps(apply_payload, indent=2, default=str),
                  "```", ""]

    body = "\n".join(lines)
    path = os.path.join(review_dir(), name)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as exc:
        raise ToolError(f"Could not write {path}: {exc}",
                        error_code="plex_rejected")

    log(f"write_review_document path={path} entries={len(entries)}")
    return {
        "ok": True,
        "path": path,
        "filename": name,
        "entries": len(entries),
        "bytes": len(body.encode("utf-8")),
        "by_problem": dict(counted.most_common()),
        "note": "Nothing has been applied. The document is a proposal.",
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
        "player": s("Room name, or a player name from list_players."),
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
