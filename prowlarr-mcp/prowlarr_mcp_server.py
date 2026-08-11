#!/usr/bin/env python3
"""
prowlarr-mcp - one search across every indexer, returning magnet links.

Prowlarr is the thing that already solved the hard half of this. It holds the
indexer definitions, it holds the credentials, and it holds the Cloudflare
problem - a gated indexer is configured with an indexer proxy inside Prowlarr,
and by the time a result reaches this server that fight is over. **This server
never touches a challenge, a cookie, or a browser.** If an indexer is blocked,
the honest answer is "that indexer is failing", and that is what it says.

Three things make this more than a REST wrapper:

  1. It returns a **magnet**, or says plainly that it cannot. This is most of
     the work. Prowlarr's `magnetUrl` is null for a great many indexers - often
     for every result of a search - and what it hands back instead is a proxy
     link to a `.torrent`. So the magnet is recovered in four stages, cheapest
     first:

         magnetUrl                    - when the indexer supplied one
         infoHash                     - rebuild from the hash
         the proxy link's `link=`     - Prowlarr Base64s the indexer's own
                                        download link into its own URL, and for
                                        a magnet-based tracker that link IS the
                                        magnet. Free, and the common case.
         fetch the .torrent           - follow the download link; either it
                                        redirects to a magnet, or the file
                                        arrives and the info hash is the SHA-1
                                        of its `info` dictionary.

     Only the last one touches the network, and only for the releases actually
     being returned. A `.torrent` URL is never returned in the magnet slot:
     everything downstream takes a magnet, so a near-miss is worse than a
     refusal - it is discovered three steps later with nothing pointing back.

  2. It **parses the title**. Raw Prowlarr search returns hundreds of rows whose
     only structure is a filename convention. Resolution, source, codec, size,
     seeders and cam-rip detection become fields, so the choosing happens
     against data instead of against a regex the agent invented in the moment.

  3. **Empty is a diagnosis, not a result.** Zero rows because nothing matched
     and zero rows because both indexers are disabled look identical over the
     wire, and they need opposite responses. When a search comes back empty this
     asks Prowlarr why and names the failing indexer.

Deliberately **search only**. No grab, no download client, no torrent state.
The magnet goes to whatever handles fetching; this server's job ends at finding
it. A search tool that can also start downloads is a much wider blast radius
than the job needs.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python prowlarr_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python prowlarr_mcp_server.py prowlarr_status
         python prowlarr_mcp_server.py list_indexers
         python prowlarr_mcp_server.py search query="the thing" kind=movie

Environment:
    PROWLARR_URL      e.g. http://127.0.0.1:9696. Default assumes Prowlarr is on
                      the same host. It is a container, but the port is
                      published, so this is a host address like any other.
    PROWLARR_API_KEY  Settings -> General -> API Key in the Prowlarr UI. Never
                      read from disk - put it in the MCP config for this server.
    PROWLARR_TIMEOUT  Seconds for a search. Default 90, and that is not
                      generous: a query that fans out to a Cloudflare-gated
                      indexer goes through a solver, and thirty seconds is an
                      ordinary result rather than a hang.
    PROWLARR_RESOLVE_TIMEOUT
                      Seconds to spend fetching one download link while deriving
                      a magnet. Default 45. Up to four run at once.
    PROWLARR_FETCH_PREFIX
                      Prepended to each magnet as a ready-to-send `fetch_command`
                      field. Default `!fetch`. Set it empty to drop the field.

No dependencies. urllib and the standard library only.
"""

import base64
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

# Own directory first, then the repo root. The copy of mcpkit.py beside this
# file is what makes prowlarr-mcp/ a directory you can drop into an MCP folder
# on its own; inside the checkout both paths resolve to the same module anyway.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://127.0.0.1:9696").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
PROWLARR_TIMEOUT = int(os.environ.get("PROWLARR_TIMEOUT", "90"))
RESOLVE_TIMEOUT = int(os.environ.get("PROWLARR_RESOLVE_TIMEOUT", "45"))
FETCH_PREFIX = os.environ.get("PROWLARR_FETCH_PREFIX", "!fetch").strip()

# A status call that hangs for the full search timeout is a status call nobody
# runs when they most need it. Prowlarr answers these from memory.
STATUS_TIMEOUT = 15

# How many download links to resolve at once. Each one makes Prowlarr reach out
# to the indexer, so this is politeness as much as speed - and the whole point
# is to overlap the waiting, not to hammer anyone.
RESOLVE_WORKERS = 4
# A .torrent for a season pack is a few hundred KB. Anything past this is not a
# torrent file and should not be read into memory to find out.
MAX_TORRENT_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def api(path, params=None, timeout=None):
    if not PROWLARR_API_KEY:
        raise ToolError(
            "PROWLARR_API_KEY is not set. Copy it from Prowlarr under "
            "Settings -> General -> API Key, and put it in the MCP config for "
            "this server. Do not look for it on disk."
        )
    url = f"{PROWLARR_URL}/api/v1/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, headers={"X-Api-Key": PROWLARR_API_KEY, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or PROWLARR_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
            return json.loads(text) if text.strip() else []
    except urllib.error.HTTPError as exc:
        # Prowlarr answers errors with {"message": ..., "description": ...},
        # where the description is a C# stack trace. The message is the only
        # part worth passing on; the trace is noise that buries it.
        body = exc.read().decode("utf-8", "replace")
        exc.close()
        try:
            detail = (json.loads(body) or {}).get("message") or body
        except ValueError:
            detail = body
        detail = detail.strip()[:300]
        if exc.code in (401, 403):
            raise ToolError(
                "Prowlarr rejected PROWLARR_API_KEY. The key is wrong, or it "
                "belongs to a different Prowlarr instance than PROWLARR_URL "
                "points at. This is a config fix, not something to retry."
            )
        if exc.code == 404:
            raise ToolError(
                f"Prowlarr has no endpoint {path!r} (404). PROWLARR_URL may be "
                "pointing at something that is not Prowlarr."
            )
        raise ToolError(f"Prowlarr returned HTTP {exc.code} for {path}: {detail or exc.reason}")
    except socket.timeout:
        raise ToolError(
            f"Prowlarr did not answer within {timeout or PROWLARR_TIMEOUT}s. A "
            "search that reaches a Cloudflare-gated indexer goes through a "
            "solver and is genuinely slow, so this may be a timeout that is too "
            "short rather than a failure. Report it and stop - retrying the same "
            "query only queues a second slow search behind the first."
        )
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Could not reach Prowlarr at {PROWLARR_URL}: {exc.reason}. Check the "
            "container is up and the port is published. Report this and stop - a "
            "URL invented here is not a fix."
        )


# ---------------------------------------------------------------------------
# Categories
#
# Newznab numbering, which every indexer Prowlarr talks to agrees on. Kept to
# the four anyone asks for out loud; an indexer that does not carry a category
# simply returns nothing for it, which is why `any` exists as an escape hatch.
# ---------------------------------------------------------------------------

CATEGORIES = {
    "movie": [2000],
    "tv": [5000],
    "anime": [5070],
    "any": [],
}


# ---------------------------------------------------------------------------
# Reading a release title
#
# The only structure a scene title has is convention, and the convention is
# stable enough to parse. Doing it here rather than in the agent means the
# comparison between two releases is between fields, not between two guesses.
# ---------------------------------------------------------------------------

RESOLUTIONS = (
    (2160, re.compile(r"(?i)\b(2160p|4k|uhd)\b")),
    (1080, re.compile(r"(?i)\b1080[pi]\b")),
    (720, re.compile(r"(?i)\b720p\b")),
    (576, re.compile(r"(?i)\b576[pi]\b")),
    (480, re.compile(r"(?i)\b480[pi]\b")),
)

# Best first. `remux` before `bluray` because a remux is a bluray, and the more
# specific label is the more useful one to report.
SOURCES = (
    ("remux", re.compile(r"(?i)\bremux\b")),
    ("bluray", re.compile(r"(?i)\b(blu-?ray|bdrip|brrip|bd(?:25|50)|bdmv)\b")),
    ("web-dl", re.compile(r"(?i)\b(web-?dl|amzn|nf|dsnp|hmax|atvp|pcok|stan)\b")),
    ("webrip", re.compile(r"(?i)\bweb-?rip\b")),
    ("hdtv", re.compile(r"(?i)\b(hdtv|pdtv)\b")),
    ("dvd", re.compile(r"(?i)\b(dvdrip|dvd-?r|dvd5|dvd9)\b")),
)

CODECS = (
    ("av1", re.compile(r"(?i)\bav1\b")),
    ("x265", re.compile(r"(?i)\b(x\.?265|h\.?265|hevc)\b")),
    ("x264", re.compile(r"(?i)\b(x\.?264|h\.?264|avc)\b")),
    ("xvid", re.compile(r"(?i)\b(xvid|divx)\b")),
)

HDR = re.compile(r"(?i)\b(hdr10\+?|hdr|dolby[. ]?vision)\b")

# Unambiguous: a title carrying one of these is a recording of a cinema screen
# whatever else it claims.
CAM_CERTAIN = re.compile(
    r"(?i)\b(cam-?rip|hd-?cam|cam|telesync|hd-?ts|telecine|screener|dvd-?scr|"
    r"work-?print|pre-?dvd)\b")
# Ambiguous two-letter tags. `TS` is telesync and is also three letters of a
# release group name, so these only count when nothing else in the title claims
# a real resolution - which is the case for every actual cam rip and almost no
# legitimate release.
CAM_MAYBE = re.compile(r"(?i)\b(ts|tc|scr)\b")


def read_title(title):
    """Turn a release title into fields. Never raises; unknown stays None."""
    resolution = next((value for value, pattern in RESOLUTIONS if pattern.search(title)), None)
    source = next((name for name, pattern in SOURCES if pattern.search(title)), None)
    codec = next((name for name, pattern in CODECS if pattern.search(title)), None)
    cam = bool(CAM_CERTAIN.search(title)) or (
        resolution in (None, 480, 576) and bool(CAM_MAYBE.search(title)))
    return {
        "resolution": resolution,
        "source": source,
        "codec": codec,
        "hdr": bool(HDR.search(title)),
        "cam": cam,
    }


def human_size(size):
    if not size:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit not in ("B", "KB") else f"{value:.0f}{unit}"
        value /= 1024


def human_age(published):
    """'3d', '6h'. Absolute dates are noise; how stale it is, is the question."""
    if not published:
        return None
    text = str(published).replace("Z", "+00:00")
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    delta = datetime.datetime.now(datetime.timezone.utc) - when
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "new"
    if hours < 48:
        return f"{int(hours)}h"
    if hours < 24 * 90:
        return f"{int(hours // 24)}d"
    return f"{int(hours // 24 // 365)}y" if hours > 24 * 365 else f"{int(hours // 24 // 30)}mo"


def build_magnet(info_hash, name, trackers=()):
    parts = [f"magnet:?xt=urn:btih:{info_hash.lower()}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(name))
    seen = []
    for tracker in trackers:
        text = tracker.decode("utf-8", "replace") if isinstance(tracker, bytes) else str(tracker)
        if text and text not in seen:
            seen.append(text)
    # Enough for a client to find peers without turning the magnet into a wall
    # of text. DHT does the rest.
    parts.extend("tr=" + urllib.parse.quote(t, safe="") for t in seen[:8])
    return "&".join(parts)


def magnet_for(release):
    """The magnet, from whatever the release actually carries. No network.

    Returns (magnet, note, download_url). Four sources, cheapest first — the
    third is the one that matters in practice, because Prowlarr's proxy link
    carries the indexer's own download link Base64-encoded inside it, and for a
    magnet-based indexer that link *is* the magnet. It just never appears in
    `magnetUrl`.

    A `.torrent` URL is deliberately never returned in the magnet slot:
    everything downstream takes a magnet, and a link that is almost one is the
    failure that gets discovered three steps later.
    """
    magnet = (release.get("magnetUrl") or "").strip()
    if magnet.startswith("magnet:"):
        return magnet, None, None

    info_hash = (release.get("infoHash") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash):
        return build_magnet(info_hash, release.get("title")), "rebuilt from the info hash", None

    download_url = (release.get("downloadUrl") or "").strip()
    for candidate in (unwrap_proxy_link(download_url), (release.get("guid") or "").strip()):
        if candidate.startswith("magnet:"):
            return candidate, "taken from the indexer's own download link", None

    return None, None, download_url or None


def unwrap_proxy_link(download_url):
    """The original indexer link out of a Prowlarr `/download?link=...` URL.

    Prowlarr wraps every download behind its own proxy so it can add
    credentials, and Base64s the real link into the query string. Decoding it
    costs nothing and saves a round trip through the indexer for every
    magnet-based tracker.
    """
    if not download_url:
        return ""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(download_url).query)
    except ValueError:
        return ""
    value = (query.get("link") or query.get("Link") or [""])[0]
    if not value:
        return ""
    try:
        # Prowlarr uses the URL-safe alphabet and strips the padding.
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Resolving a magnet from the .torrent itself
#
# The universal fallback, and the reason this server can promise a magnet at
# all. A magnet is an info hash plus a name plus trackers, and every one of
# those is inside the .torrent file — the info hash being the SHA-1 of the
# `info` dictionary exactly as it appears on the wire. So the bytes have to be
# hashed in place; decoding and re-encoding would produce a different hash for
# any file whose encoder ordered keys differently, and that hash would be
# silently wrong rather than obviously broken.
# ---------------------------------------------------------------------------


def bdecode(data, start=0):
    """Minimal bencode reader. Returns (value, index after it)."""
    char = data[start:start + 1]
    if char == b"i":
        end = data.index(b"e", start)
        return int(data[start + 1:end]), end + 1
    if char == b"l":
        out, index = [], start + 1
        while data[index:index + 1] != b"e":
            value, index = bdecode(data, index)
            out.append(value)
        return out, index + 1
    if char == b"d":
        out, index = {}, start + 1
        while data[index:index + 1] != b"e":
            key, index = bdecode(data, index)
            value, index = bdecode(data, index)
            out[key] = value
        return out, index + 1
    if char.isdigit():
        colon = data.index(b":", start)
        length = int(data[start:colon])
        end = colon + 1 + length
        return data[colon + 1:end], end
    raise ValueError(f"not bencoded data at byte {start}")


def info_span(data):
    """Where the `info` dictionary starts and ends in the raw bytes."""
    if data[0:1] != b"d":
        raise ValueError("a .torrent is a bencoded dictionary; this is not one")
    index = 1
    while data[index:index + 1] != b"e":
        key, index = bdecode(data, index)
        start = index
        _value, index = bdecode(data, index)
        if key == b"info":
            return start, index
    raise ValueError("no info dictionary in this .torrent")


def magnet_from_torrent(data):
    start, end = info_span(data)
    info_hash = hashlib.sha1(data[start:end]).hexdigest()
    info, _ = bdecode(data, start)
    meta, _ = bdecode(data)
    name = (info.get(b"name") or b"").decode("utf-8", "replace")
    trackers = [meta[b"announce"]] if meta.get(b"announce") else []
    for tier in meta.get(b"announce-list") or []:
        trackers.extend(tier if isinstance(tier, list) else [tier])
    return build_magnet(info_hash, name, trackers)


class KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow. A redirect to `magnet:` is the answer, not a detour.

    urllib cannot open a `magnet:` URL and would raise `unknown url type`,
    which reads like a bug in this server rather than the success it is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_download(url):
    """Follow a Prowlarr download link. Returns ('magnet', str) or ('torrent', bytes)."""
    opener = urllib.request.build_opener(KeepRedirect)
    request = urllib.request.Request(url, headers={"X-Api-Key": PROWLARR_API_KEY})
    try:
        with opener.open(request, timeout=RESOLVE_TIMEOUT) as resp:
            return "torrent", resp.read(MAX_TORRENT_BYTES)
    except urllib.error.HTTPError as exc:
        location = (exc.headers.get("Location") or "") if exc.headers else ""
        exc.close()
        if 300 <= exc.code < 400 and location.startswith("magnet:"):
            return "magnet", location
        raise


def resolve_magnet(row):
    """Turn one magnet-less row into a magnet, over the network. Never raises."""
    try:
        kind, payload = fetch_download(row["download_url"])
        if kind == "magnet":
            row["magnet"] = payload
            row["magnet_note"] = "followed the download link to a magnet"
        else:
            row["magnet"] = magnet_from_torrent(payload)
            row["magnet_note"] = "computed from the .torrent file"
    except Exception as exc:
        # One release failing to resolve must not fail the search. The row keeps
        # magnet: null and says why, which is exactly what it said before.
        row["magnet_note"] = (
            f"no magnet from this indexer, and its .torrent could not be read "
            f"({type(exc).__name__}). Pick another release.")
    return row


# ---------------------------------------------------------------------------
# Indexers
# ---------------------------------------------------------------------------


def all_indexers(timeout=None):
    rows = api("indexer", timeout=timeout or STATUS_TIMEOUT)
    return rows if isinstance(rows, list) else []


def failures(timeout=None):
    """Indexer id -> why Prowlarr has given up on it, for the ones it has."""
    out = {}
    for row in api("indexerstatus", timeout=timeout or STATUS_TIMEOUT) or []:
        reason = row.get("mostRecentFailure") or row.get("initialFailure")
        until = row.get("disabledTill")
        out[row.get("indexerId")] = (
            f"failing since {reason}" if reason else "failing"
        ) + (f", backed off until {until}" if until else "")
    return out


def resolve_indexer(name):
    """A name the human said -> the id Prowlarr wants. Exact, prefix, substring.

    The agent never sends an indexer id, for the same reason it never sends an
    entity id anywhere else in this repo: the id is an implementation detail
    that changes when you delete and re-add an indexer, and the name does not.
    """
    rows = all_indexers()
    if not rows:
        raise ToolError(
            "Prowlarr has no indexers configured at all. Add at least one in "
            "the Prowlarr UI under Indexers -> Add Indexer. Nothing here can "
            "work around that."
        )
    want = str(name).strip().lower()
    enabled = [r for r in rows if r.get("enable")]
    for row in rows:
        if (row.get("name") or "").lower() == want:
            return row
    matches = [r for r in rows if (r.get("name") or "").lower().startswith(want)] or \
              [r for r in rows if want in (r.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(f"{name!r} matches more than one indexer. Say which.",
                        candidates=sorted((r.get("name") or "?") for r in matches))
    raise ToolError(
        f"Prowlarr has no indexer called {name!r}. Use a name from list_indexers "
        "verbatim, or omit indexer= to search all of them.",
        enabled_indexers=sorted((r.get("name") or "?") for r in enabled),
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@tool(
    "Check the connection, the API key, and the health of every indexer. Run "
    "this first when a search comes back empty or looks wrong — it names the "
    "indexers Prowlarr has given up on, which is the usual cause and is not "
    "something a different query fixes."
)
def prowlarr_status():
    info = api("system/status", timeout=STATUS_TIMEOUT)
    rows = all_indexers()
    broken = failures()

    enabled = [r for r in rows if r.get("enable")]
    failing = [f"{r.get('name')} ({broken[r['id']]})" for r in enabled if r.get("id") in broken]
    healthy = [r.get("name") for r in enabled if r.get("id") not in broken]
    disabled = [r.get("name") for r in rows if not r.get("enable")]

    summary = (f"Prowlarr {info.get('version', '?')} at {PROWLARR_URL}. "
               f"{len(enabled)} of {len(rows)} indexers enabled.")
    problems = []
    if not rows:
        problems.append("No indexers are configured. Add one in the Prowlarr UI.")
    elif not enabled:
        problems.append("Every indexer is disabled. Nothing can be searched.")
    if failing:
        summary += f" {len(failing)} FAILING: {'; '.join(failing)}."
        problems.append(
            "Some indexers are failing. A Cloudflare-gated indexer failing here "
            "means its indexer proxy in Prowlarr is misconfigured or down — fix "
            "it in Prowlarr, not from this side.")
    if healthy:
        summary += f" Working: {', '.join(healthy)}."
    if disabled:
        summary += f" Disabled: {', '.join(disabled)}."

    return {
        "ok": not problems,
        "summary": summary,
        "version": info.get("version"),
        "url": PROWLARR_URL,
        "indexers_enabled": healthy,
        "indexers_failing": failing,
        "indexers_disabled": disabled,
        "error": " ".join(problems) or None,
    }


@tool(
    "List the indexers this server can search, what each one carries, and "
    "whether it is healthy. Use it when an indexer name is rejected, or to see "
    "what is actually available before narrowing a search to one of them."
)
def list_indexers():
    rows = all_indexers()
    broken = failures()
    if not rows:
        raise ToolError(
            "Prowlarr has no indexers configured. Add one in the Prowlarr UI "
            "under Indexers -> Add Indexer, then run prowlarr_status."
        )

    listed = []
    for row in sorted(rows, key=lambda r: (r.get("name") or "").lower()):
        caps = ((row.get("capabilities") or {}).get("categories") or [])
        carries = sorted({c.get("name") for c in caps
                          if c.get("id") in (2000, 5000) and c.get("name")})
        listed.append({
            "name": row.get("name"),
            "protocol": row.get("protocol"),
            "privacy": row.get("privacy"),
            "enabled": bool(row.get("enable")),
            "carries": carries,
            "health": broken.get(row.get("id"), "ok" if row.get("enable") else "disabled"),
        })

    parts = [f"{r['name']} ({r['protocol']}, {r['health']})" for r in listed]
    return {
        "ok": True,
        "summary": f"{len(listed)} indexer(s): " + "; ".join(parts)
                   + ". Use these names verbatim in search(indexer=...).",
        "indexers": listed,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def build_query(query, year=None, season=None, episode=None):
    """Fold season, episode and year into the query text.

    Prowlarr has typed `tvsearch` and `moviesearch` modes that take these as
    separate parameters, and they are the better interface right up until an
    indexer does not advertise the capability — at which point the search
    silently returns nothing at all. Scene naming is universal and a plain text
    search works against every indexer, so the string is built here instead.
    """
    text = str(query).strip()
    if not text:
        raise ToolError("query is empty. Pass the title to look for.")
    if episode is not None and season is None:
        raise ToolError(
            f"episode={episode} was given without a season, and an episode "
            "number means nothing on its own. Pass season= as well."
        )
    if season is not None:
        text += f" S{int(season):02d}" + (f"E{int(episode):02d}" if episode is not None else "")
    elif year is not None:
        # Only when it is not an episode search. "Show S01 2019" matches far
        # less than "Show S01", because the year in a TV title is the series
        # year and rarely appears in the release name at all.
        text += f" {int(year)}"
    return text


TIER = {2160: 5, 1080: 4, 720: 3, 576: 2, 480: 1}


def rank_key(row, sort):
    if sort == "seeders":
        return (row.get("seeders") or 0,)
    if sort == "size":
        return (row.get("size_bytes") or 0,)
    if sort == "newest":
        return (row.get("published") or "",)
    # "best": never a cam, then the higher resolution, then whichever of those
    # will actually finish downloading. Resolution outranks seeders on purpose -
    # a healthy 480p rip is not the answer to a request for a film.
    return (not row["cam"], TIER.get(row["resolution"], 2), row.get("seeders") or 0)


def collapse(rows):
    """One row per release, not one per indexer carrying it.

    The same torrent listed on three indexers is three rows with identical info
    hashes, and presenting it three times makes a list of ten results really a
    list of four. Keep the healthiest copy and record where else it lives.
    """
    out = {}
    for row in rows:
        key = row.get("info_hash") or (row.get("title"), row.get("size_bytes"))
        existing = out.get(key)
        if existing is None:
            out[key] = row
            continue
        keep, drop = ((existing, row) if (existing.get("seeders") or 0) >= (row.get("seeders") or 0)
                      else (row, existing))
        keep["also_on"] = sorted(set(keep.get("also_on", []) + drop.get("also_on", [])
                                     + [drop["indexer"]]) - {keep["indexer"]})
        out[key] = keep
    return list(out.values())


@tool(
    "Search every configured indexer at once and return ranked releases with "
    "magnet links. This is the only supported way to find something — do not "
    "browse an indexer site, and do not call the Prowlarr API directly.\n\n"
    "**A search can take 30-60 seconds.** Indexers behind a challenge are "
    "solved inside Prowlarr, which is slow and is working as intended. Wait for "
    "it. Do not fire a second search because the first is taking a while.\n\n"
    "Results carry parsed fields — resolution, source, codec, size, seeders — "
    "so pick against those rather than by reading the title. `cam: true` marks "
    "a recording of a cinema screen; it is never the right answer.",
    {"query": s("The title to look for. Just the name — no resolution, no "
                "release group, no year. Extra words narrow the search against "
                "the indexer's own matching and usually return nothing."),
     "kind": s("Narrows to a category. Use 'any' if a narrowed search comes "
               "back empty — not every indexer files things the same way.",
               default="any", enum=["movie", "tv", "anime", "any"]),
     "season": i("Season number, for television.", minimum=0),
     "episode": i("Episode number. Requires season. Omit both for a whole "
                  "series, or give season alone for a season pack.", minimum=0),
     "year": i("Release year, to separate remakes. Ignored when season is "
               "given, where it does more harm than good.", minimum=1900,
               maximum=2100),
     "indexer": s("Restrict to one indexer, named exactly as list_indexers "
                  "reports it. Omit to search all of them, which is normal."),
     "min_seeders": i("Drop releases with fewer seeders than this. 1 means "
                      "'someone is sharing it'. Raise it when results look "
                      "healthy but nothing downloads.", default=1, minimum=0),
     "limit": i("How many releases to return after ranking.", default=10,
                minimum=1, maximum=50),
     "sort": s("How to rank. 'best' is resolution then health, and is what you "
               "want unless asked otherwise.", default="best",
               enum=["best", "seeders", "size", "newest"]),
     "resolve_magnets": b("Fetch the download link for any returned release "
                          "that has no magnet, and derive one. Leave this on — "
                          "without it many indexers return nothing usable. Turn "
                          "it off only to browse titles quickly.", default=True)},
    required=["query"],
)
def search(query, kind="any", season=None, episode=None, year=None, indexer=None,
           min_seeders=1, limit=10, sort="best", resolve_magnets=True):
    kind = (kind or "any").strip().lower()
    if kind not in CATEGORIES:
        raise ToolError(f"kind must be one of {', '.join(sorted(CATEGORIES))}, got {kind!r}.")
    sort = (sort or "best").strip().lower()

    text = build_query(query, year=year, season=season, episode=episode)
    params = {"query": text, "type": "search"}
    if kind != "any":
        params["categories"] = CATEGORIES[kind]

    scoped = None
    if indexer:
        scoped = resolve_indexer(indexer)
        if not scoped.get("enable"):
            raise ToolError(
                f"The indexer {scoped.get('name')!r} is disabled in Prowlarr, so "
                "searching it would return nothing. Enable it in the Prowlarr UI "
                "or omit indexer= to search the rest.")
        params["indexerIds"] = [scoped["id"]]
        asked = [scoped]
    else:
        # Every enabled indexer, named explicitly. There is no "search
        # everything" sentinel: Prowlarr filters its indexers down to the ids it
        # was given, so a value matching nothing - `-1`, say - produces zero
        # indexers and an HTTP 400 reading "all selected indexers being
        # unavailable", which describes the wrong problem entirely.
        asked = [r for r in all_indexers() if r.get("enable")]
        if not asked:
            raise ToolError(
                "No enabled indexers, so there is nothing to search. Enable one "
                "in the Prowlarr UI under Indexers, then run prowlarr_status.")
        params["indexerIds"] = [r["id"] for r in asked]

    try:
        raw = api("search", params)
    except ToolError as exc:
        if "unavailable" not in str(exc).lower():
            raise
        # Prowlarr refused to search at all. Every indexer it was given is
        # backed off after repeated failures, which is a fact about the
        # indexers and not about the query.
        broken = failures()
        named = [f"{r.get('name')} ({broken[r['id']]})" for r in asked if r.get("id") in broken]
        raise ToolError(
            f"Prowlarr refused the search: every indexer asked is unavailable. "
            + (f"{'; '.join(named)}. " if named else "")
            + "Prowlarr backs an indexer off after repeated failures, and for a "
              "challenged indexer that usually means its proxy is down or "
              "untagged. Fix it in the Prowlarr UI and use its Test button. "
              "Report this and stop - no query can work around it.",
            indexers_asked=[r.get("name") for r in asked],
        )
    if not isinstance(raw, list):
        raise ToolError(f"Prowlarr returned something unexpected for a search: {raw!r:.200}")

    rows = []
    for release in raw:
        seeders = release.get("seeders")
        # Usenet has no seeders at all, so `None` must not be read as zero and
        # filtered away. Only an actual number is compared.
        if isinstance(seeders, int) and seeders < min_seeders:
            continue
        magnet, note, download_url = magnet_for(release)
        parsed = read_title(release.get("title") or "")
        rows.append(dict(parsed, **{
            "title": release.get("title"),
            "indexer": release.get("indexer"),
            "protocol": release.get("protocol"),
            "seeders": seeders,
            "leechers": release.get("leechers"),
            "size": human_size(release.get("size")),
            "size_bytes": release.get("size"),
            "age": human_age(release.get("publishDate")),
            "published": release.get("publishDate"),
            "info_hash": (release.get("infoHash") or "").lower() or None,
            "magnet": magnet,
            "magnet_note": note,
            "download_url": download_url,
            "also_on": [],
        }))

    rows = collapse(rows)
    rows.sort(key=lambda r: rank_key(r, sort), reverse=True)
    rows = rows[:limit]

    # Only now, and only for what is actually being returned. Each of these
    # makes Prowlarr reach out to the indexer, so resolving all 63 matches to
    # hand back 10 would be sixty wasted round trips through a solver.
    pending = [r for r in rows if r["magnet"] is None and r["download_url"]]
    if resolve_magnets and pending:
        workers = min(RESOLVE_WORKERS, len(pending))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(resolve_magnet, pending))
    elif pending:
        for row in pending:
            row["magnet_note"] = ("no magnet from this indexer, and resolve_magnets "
                                  "was off, so none was derived")

    unusable = sum(1 for r in rows if r["magnet"] is None)
    for rank, row in enumerate(rows, 1):
        row["n"] = rank
        if FETCH_PREFIX and row["magnet"]:
            row["fetch_command"] = f"{FETCH_PREFIX} {row['magnet']}"

    if not rows:
        return no_results(text, kind, scoped, min_seeders, len(raw))

    lines = []
    for row in rows:
        bits = [part for part in (
            f"{row['resolution']}p" if row["resolution"] else None,
            row["source"], row["codec"], "HDR" if row["hdr"] else None, row["size"],
            f"{row['seeders']} seeders" if isinstance(row["seeders"], int) else None,
            row["age"], row["indexer"]) if part]
        warn = " ** CAM — do not use **" if row["cam"] else (
            f" ** {row['magnet_note']} **" if row["magnet"] is None else "")
        lines.append(f"{row['n']}. {row['title']}\n     " + " · ".join(bits) + warn)

    summary = f"{len(rows)} release(s) for {text!r}" \
              + (f" on {scoped['name']}" if scoped else "") + ":\n  " + "\n  ".join(lines)
    if unusable:
        summary += (f"\n\n{unusable} of these has no usable magnet and cannot be "
                    "handed off — the note on the row says why. Pick one of the "
                    "others.")

    return {
        "ok": True,
        "summary": summary,
        "query_sent": text,
        "results": rows,
        "total_before_ranking": len(raw),
        "without_magnet": unusable,
        "note": f"{len(rows) - unusable} of {len(rows)} results carry a magnet in "
                "`magnet`" + (" with a ready-to-send line in `fetch_command`."
                              if FETCH_PREFIX else "."),
    }


def no_results(text, kind, scoped, min_seeders, raw_count):
    """Empty, with a reason. The reasons need opposite responses.

    Nothing matched, everything was filtered out, and every indexer is broken
    are indistinguishable from a count of zero, and only the first is a cue to
    try a different title.
    """
    if raw_count:
        return {
            "ok": True,
            "summary": f"{raw_count} release(s) matched {text!r} but none had at "
                       f"least {min_seeders} seeder(s). Lower min_seeders to see "
                       "them, but understand what that means: nothing is sharing "
                       "them and a download will not start.",
            "query_sent": text, "results": [], "total_before_ranking": raw_count,
        }

    broken = failures()
    # search() has already refused the no-enabled-indexers case, so this list is
    # never empty by the time it gets here.
    relevant = [scoped] if scoped else [r for r in all_indexers() if r.get("enable")]
    down = [f"{r.get('name')} ({broken[r['id']]})" for r in relevant if r.get("id") in broken]

    if down and len(down) == len(relevant):
        return {
            "ok": False,
            "error": f"Nothing found for {text!r}, and that is not a fact about the "
                     f"title: every indexer searched is failing — {'; '.join(down)}. "
                     "Report this and stop. A different query cannot fix a broken "
                     "indexer, and neither can searching the site directly.",
            "query_sent": text, "results": [],
        }
    summary = f"Nothing found for {text!r}."
    if down:
        summary += (f" {len(down)} of {len(relevant)} indexers searched are failing "
                    f"({'; '.join(down)}), so this may be incomplete rather than absent.")
    if kind != "any":
        summary += f" The search was narrowed to {kind}; try kind=any before trying another title."
    return {"ok": True, "summary": summary, "query_sent": text, "results": [],
            "indexers_failing": down}


def banner():
    return (f"PROWLARR_URL={PROWLARR_URL}  "
            f"PROWLARR_API_KEY={'set' if PROWLARR_API_KEY else 'MISSING'}  "
            f"PROWLARR_TIMEOUT={PROWLARR_TIMEOUT}s  "
            f"PROWLARR_RESOLVE_TIMEOUT={RESOLVE_TIMEOUT}s  "
            f"fetch prefix={FETCH_PREFIX or '(none)'}")


if __name__ == "__main__":
    run("prowlarr-mcp", "1.0", banner)
