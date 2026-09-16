#!/usr/bin/env python3
"""
qbt-mcp - start a download in qBittorrent, and confirm it actually started.

`prowlarr-mcp` deliberately stops at finding a magnet. This is the other end,
and they stay apart on purpose: a search tool that can also start downloads has
a much wider blast radius than searching needs.

This talks to qBittorrent's Web UI API directly. There is no bot, no chat
message, and no relay in the middle - the fewer processes between deciding to
download something and it downloading, the fewer ways an evening ends with
nothing having happened.

Three things it does beyond wrapping the API:

  1. **It files things in the right library.** A magnet's `dn` parameter is a
     release name, and release names say whether they are an episode. `S02E05`,
     `1x02`, `Season 3` and `S01.Complete` all mean television; anything else is
     treated as a film. Shows go to QBT_SHOWS_PATH, films to QBT_MOVIES_PATH.
     Pass `kind` explicitly when you can see the detector is about to be wrong -
     it is a regex over a filename convention, not a judgement.

  2. **It confirms.** `torrents/add` returns `Ok.` when the request was
     *accepted*, which it is for a magnet with no seeders, a malformed hash, and
     a healthy release alike. So every add is followed by a read-back against
     `torrents/info` until the torrent appears, and what gets reported is the
     state qBittorrent actually holds. This is the §3 convention from DESIGN.md
     and it is the whole difference between "downloading" and "sent".

  3. **A stall is named, not averaged in.** A torrent sitting at 0% in
     `stalledDL` has found no seeders and is not going to finish on its own.
     Reporting that alongside real progress is how someone waits all evening for
     a file that was never coming.

A fourth thing, for the case the other three don't cover: some private
trackers (BTN among them) disable DHT and PEX, so a magnet with no embedded
tracker URL has no way to find peers at all - it isn't slow, it is stuck, and
sits at 0% in `metaDL` forever. `download_torrent` is the backup path: it hands
qBittorrent the raw `.torrent` file instead of a magnet, which carries its own
tracker list and needs no discovery. Same filing, same read-back confirmation
as `download` - just a different way in when a magnet is visibly never going to
resolve.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python qbt_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python qbt_mcp_server.py qbt_status
         python qbt_mcp_server.py download magnet="magnet:?xt=urn:btih:..."
         python qbt_mcp_server.py download_torrent torrent_path="C:/tmp/x.torrent"
         python qbt_mcp_server.py downloads
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

QBT_URL = os.environ.get("QBT_URL", "http://127.0.0.1:8080").rstrip("/")
QBT_USER = os.environ.get("QBT_USER", "admin")
QBT_PASS = os.environ.get("QBT_PASS", "")
QBT_TIMEOUT = int(os.environ.get("QBT_TIMEOUT", "30"))

# Where each kind of thing lands. No defaults: a wrong path does not fail, it
# silently files a season pack into the film library, and Plex indexes it before
# anyone notices.
MOVIES_PATH = os.environ.get("QBT_MOVIES_PATH", "").strip()
SHOWS_PATH = os.environ.get("QBT_SHOWS_PATH", "").strip()

# How long to wait for an added torrent to show up before reporting it
# unconfirmed. A magnet registers as soon as qBittorrent parses it - well before
# any metadata arrives - so this is generous.
CONFIRM_TIMEOUT = int(os.environ.get("QBT_CONFIRM_TIMEOUT", "10"))

# A .torrent for a season pack is a few hundred KB. Anything past this is not a
# torrent file and should not be read into memory to find out.
MAX_TORRENT_BYTES = 4 * 1024 * 1024

TV_PATTERNS = [
    r'S\d{1,2}E\d{1,2}',                # S01E02
    r'\d{1,2}x\d{2}',                   # 1x02
    r'Season[\s.]\d+',                  # Season 1 / Season.1
    r'S\d{1,2}[\s.]?(Complete|Pack)',   # S01.Complete
    r'\bS\d{1,2}\b',                    # S01 / S1 (standalone season)
]

# States meaning "qBittorrent is trying". Anything else is finished, paused, or
# broken, and each of those needs saying rather than glossing.
ACTIVE_STATES = ("downloading", "stalledDL", "queuedDL", "metaDL", "forcedDL", "checkingDL")

_opener = None


def client():
    """
    A logged-in opener, built once.

    qBittorrent authenticates with a session cookie, so the cookie jar is the
    session. Login is skipped entirely when the Web UI has host-based auth
    bypass on, which is the common local setup - a 403 on login with no password
    set means the bypass is doing its job, not that anything is wrong.
    """
    global _opener
    if _opener is not None:
        return _opener

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    data = urllib.parse.urlencode({"username": QBT_USER, "password": QBT_PASS}).encode()
    req = urllib.request.Request(f"{QBT_URL}/api/v2/auth/login", data=data)
    # qBittorrent rejects cross-site requests; without this every call is a 403.
    req.add_header("Referer", QBT_URL)
    req.add_header("Origin", QBT_URL)

    try:
        with opener.open(req, timeout=QBT_TIMEOUT) as resp:
            body = resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ToolError(
                f"qBittorrent refused the login for user {QBT_USER!r} - too many "
                f"failed attempts, or the IP is banned. Check Options -> Web UI, "
                f"and clear the ban list if needed."
            )
        raise ToolError(f"qBittorrent returned HTTP {exc.code} on login.")
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Cannot reach qBittorrent at {QBT_URL} ({exc.reason}). Either it is "
            f"not running, or its Web UI is off - Tools -> Options -> Web UI -> "
            f"'Web User Interface (Remote control)'. Nothing can be downloaded "
            f"until that is on, and no other tool here can work around it."
        )

    if body.lower().startswith("fails"):
        raise ToolError(
            f"qBittorrent rejected the credentials for user {QBT_USER!r}. Fix "
            f"QBT_USER / QBT_PASS in the qbt entry of config.yaml. This is final "
            f"- do not retry."
        )

    _opener = opener
    return _opener


def _send(req, label):
    """Open `req` through the logged-in opener. Shared by `api` and
    `api_upload`, so the two ways of talking to qBittorrent - form fields for a
    magnet, a file part for a `.torrent` - fail the same way."""
    opener = client()
    try:
        with opener.open(req, timeout=QBT_TIMEOUT) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ToolError(
                "qBittorrent rejected the request as unauthorised - the session "
                "expired. Retry once; if it happens again, check QBT_USER / QBT_PASS."
            )
        raise ToolError(f"qBittorrent returned HTTP {exc.code} for {label}.")
    except urllib.error.URLError as exc:
        raise ToolError(f"Lost the connection to qBittorrent at {QBT_URL} ({exc.reason}).")


def api(path, params=None, method="GET"):
    """Call the Web UI API. Returns parsed JSON, or raw text when not JSON."""
    url = f"{QBT_URL}/api/v2/{path}"
    data = None

    if method == "POST":
        data = urllib.parse.urlencode(params or {}).encode()
    elif params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Referer", QBT_URL)
    req.add_header("Origin", QBT_URL)

    raw = _send(req, path).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def multipart_body(fields, filename, file_bytes):
    """A `multipart/form-data` body carrying `fields` plus one file part named
    `torrents` - the field qBittorrent's own upload form uses."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
            f'\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
        f'filename="{filename}"\r\nContent-Type: application/x-bittorrent\r\n\r\n'
        .encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def api_upload(path, fields, filename, file_bytes):
    """Call the Web UI API with a file attached.

    `torrents/add` takes a magnet as a plain form field (`urls`), but a
    `.torrent` can only go in as a file part - and that is what preserves its
    embedded tracker list, rather than this server trying to reconstruct one.
    """
    body, boundary = multipart_body(fields, filename, file_bytes)
    req = urllib.request.Request(f"{QBT_URL}/api/v2/{path}", data=body, method="POST")
    req.add_header("Referer", QBT_URL)
    req.add_header("Origin", QBT_URL)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    return _send(req, path).strip() or None


def detect_kind(name):
    """'show' if the release name looks like television, else 'movie'."""
    for pattern in TV_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return "show"
    return "movie"


def magnet_name(magnet):
    """The `dn` parameter, which is the release name. 'Unknown' when absent."""
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
        dn = params.get("dn", [])
        return urllib.parse.unquote(dn[0]) if dn else "Unknown"
    except Exception:
        return "Unknown"


def magnet_hash(magnet):
    """
    The v1 infohash as lowercase hex, or None.

    This is what makes an add checkable afterwards: `torrents/add` does not
    return an id, so without reading the hash out of the magnet there is nothing
    to look the torrent up by. Both encodings in the wild are handled - 40-char
    hex, and the 32-char base32 some indexers still emit.
    """
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
        for xt in params.get("xt", []):
            if not xt.lower().startswith("urn:btih:"):
                continue
            value = xt[len("urn:btih:"):]
            if len(value) == 40:
                int(value, 16)              # reject anything that is not hex
                return value.lower()
            if len(value) == 32:
                return base64.b32decode(value.upper()).hex()
    except Exception:
        pass
    return None


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


def torrent_info_hash_and_name(data):
    """The v1 infohash and release name read out of raw `.torrent` bytes.

    The hash is the SHA-1 of the `info` dictionary exactly as it appears on the
    wire - decoding and re-encoding would produce a different hash for any file
    whose encoder ordered keys differently, and that hash would be silently
    wrong rather than obviously broken.
    """
    start, end = info_span(data)
    info_hash = hashlib.sha1(data[start:end]).hexdigest()
    info, _ = bdecode(data, start)
    name = (info.get(b"name") or b"").decode("utf-8", "replace").strip()
    return info_hash, name or "Unknown"


class KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow. A redirect to `magnet:` is the answer, not a detour -
    urllib cannot open a `magnet:` URL and would raise `unknown url type`,
    which reads like a bug in this server rather than the news it actually is:
    this release never needed the torrent-file path at all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_torrent(url):
    """The raw bytes of a `.torrent` at `url`. Never touches qBittorrent's own
    session - this is an arbitrary URL, not the Web UI."""
    opener = urllib.request.build_opener(KeepRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=QBT_TIMEOUT) as resp:
            data = resp.read(MAX_TORRENT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        location = (exc.headers.get("Location") or "") if exc.headers else ""
        exc.close()
        if 300 <= exc.code < 400 and location.startswith("magnet:"):
            raise ToolError(
                f"{url} redirects to a magnet, not a .torrent file - this "
                f"release never needed the torrent-file path. Use `download` "
                f"with that magnet instead:\n{location}"
            )
        raise ToolError(f"Fetching {url} returned HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        raise ToolError(f"Could not fetch {url}: {exc.reason}.")

    if len(data) > MAX_TORRENT_BYTES:
        raise ToolError(
            f"{url} served more than {MAX_TORRENT_BYTES // 1024 // 1024}MB - "
            f"that is not a .torrent file."
        )
    return data


def read_torrent_file(path):
    """The raw bytes of a `.torrent` already on disk."""
    if not os.path.isfile(path):
        raise ToolError(
            f"No file at {path!r}. This path is read by qbt-mcp's own process, "
            f"not by whatever asked for the download - check it is readable "
            f"from there."
        )
    if os.path.getsize(path) > MAX_TORRENT_BYTES:
        raise ToolError(f"{path} is larger than a .torrent file should be.")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise ToolError(f"Could not read {path}: {exc}.")


def describe(t):
    """One torrent as a flat dict, in units a person reads."""
    return {
        "name": t.get("name"),
        "infohash": t.get("hash"),
        "state": t.get("state"),
        "progress_pct": round(t.get("progress", 0) * 100, 1),
        "speed_mbps": round(t.get("dlspeed", 0) / 1024 / 1024, 2),
        "size_gb": round(t.get("size", 0) / 1024 / 1024 / 1024, 2),
        "seeders": t.get("num_seeds"),
        "save_path": t.get("save_path"),
        "eta_minutes": (
            round(t["eta"] / 60) if t.get("eta") and t["eta"] < 8640000 else None
        ),
    }


def stall_note(rows):
    """Name the torrents that have found nobody, or return ''."""
    stalled = [r for r in rows if r["state"] == "stalledDL" and r["progress_pct"] == 0]
    if not stalled:
        return ""
    names = ", ".join(r["name"] for r in stalled)
    return (
        f" {len(stalled)} of these has found no seeders and is not going to "
        f"finish on its own ({names}) - say so and offer a different release."
        if len(stalled) == 1 else
        f" {len(stalled)} of these have found no seeders and are not going to "
        f"finish on their own ({names}) - say so and offer different releases."
    )


@tool(
    "Check that qBittorrent is reachable, that the credentials work, and that "
    "both library paths are configured. Run this first when a download fails - "
    "an unreachable client and a bad release look nothing alike and need "
    "opposite responses."
)
def qbt_status():
    version = api("app/version")
    rows = api("torrents/info") or []
    active = [r for r in rows if r.get("state") in ACTIVE_STATES]

    missing = [n for n, v in (("QBT_MOVIES_PATH", MOVIES_PATH),
                              ("QBT_SHOWS_PATH", SHOWS_PATH)) if not v]
    if missing:
        return {
            "ok": False,
            "summary": (
                f"qBittorrent {version} is reachable, but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set, so downloads "
                f"cannot be filed. Set {'it' if len(missing) == 1 else 'them'} in "
                f"the qbt entry of config.yaml. Report this and stop."
            ),
            "version": version, "movies_path": MOVIES_PATH, "shows_path": SHOWS_PATH,
        }

    return {
        "ok": True,
        "summary": (
            f"qBittorrent {version} reachable at {QBT_URL}. "
            f"{len(active)} active, {len(rows)} total. "
            f"Films -> {MOVIES_PATH}, television -> {SHOWS_PATH}."
        ),
        "version": version,
        "active": len(active),
        "total": len(rows),
        "movies_path": MOVIES_PATH,
        "shows_path": SHOWS_PATH,
    }


@tool(
    "Start downloading a magnet, and confirm it registered.\n\n"
    "Whether it is a film or television is worked out from the release name, "
    "and that decides which library it lands in. Only pass `kind` when you can "
    "see the detector is about to get it wrong - a film with a year that reads "
    "like an episode number, a documentary series, a title with 'Season' in it. "
    "Do not pass it routinely.\n\n"
    "Pass the magnet exactly as the search returned it. A result whose magnet is "
    "null cannot be used - pick another release rather than building one, and "
    "never pass a download_url here.\n\n"
    "This reads the torrent back after adding, so the reported state is what "
    "qBittorrent actually holds rather than what was requested.",
    {
        "magnet": s("The full magnet link, verbatim from the search result. "
                    "Must start with `magnet:`."),
        "kind": s("Override the film/television detection. Only when it is "
                  "visibly about to be wrong.", default="auto",
                  enum=["auto", "movie", "show"]),
    },
    ["magnet"],
)
def download(magnet, kind="auto"):
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:"):
        raise ToolError(
            "That is not a magnet link - it must start with `magnet:`. Never "
            "pass a download_url or a .torrent link here, and never assemble a "
            "magnet by hand. Pick a release whose magnet field is set."
        )

    if not MOVIES_PATH or not SHOWS_PATH:
        raise ToolError(
            "QBT_MOVIES_PATH and QBT_SHOWS_PATH must both be set before anything "
            "can be downloaded, or releases get filed in the wrong library. Set "
            "them in the qbt entry of config.yaml. This is final."
        )

    name = magnet_name(magnet)
    if kind == "auto":
        kind = detect_kind(name)
    save_path = SHOWS_PATH if kind == "show" else MOVIES_PATH
    infohash = magnet_hash(magnet)

    result = api("torrents/add", {"urls": magnet, "savepath": save_path}, method="POST")
    if isinstance(result, str) and result.strip().lower().startswith("fail"):
        raise ToolError(
            f"qBittorrent refused the magnet ({result.strip()}). It is malformed "
            f"or the save path {save_path!r} is not writable. Check the path "
            f"before trying another release."
        )

    label = "television" if kind == "show" else "film"

    # `Ok.` means accepted, not added. Read it back.
    if not infohash:
        return {
            "ok": True,
            "confirmed": False,
            "summary": (
                f"Handed {name!r} to qBittorrent as {label}, saving to {save_path}. "
                f"The magnet carries no readable infohash, so this could not be "
                f"confirmed - check `downloads` to see whether it registered."
            ),
            "name": name, "kind": kind, "save_path": save_path, "infohash": None,
        }

    return _confirm(name, kind, save_path, infohash)


def _confirm(name, kind, save_path, infohash, via="", extra=None):
    """Read an add back until it appears, and report what qBittorrent actually
    holds rather than what was requested. Shared by `download` and
    `download_torrent` - the §3 convention from DESIGN.md, applied once."""
    label = "television" if kind == "show" else "film"
    base = {"name": name, "kind": kind, "save_path": save_path, "infohash": infohash}
    base.update(extra or {})

    deadline = time.time() + CONFIRM_TIMEOUT
    found = None
    while time.time() < deadline:
        rows = api("torrents/info", {"hashes": infohash}) or []
        if rows:
            found = rows[0]
            break
        time.sleep(0.5)

    if not found:
        return {
            "ok": True,
            "confirmed": False,
            "summary": (
                f"qBittorrent accepted {name!r} but it has not appeared after "
                f"{CONFIRM_TIMEOUT}s. It may be a duplicate of something already "
                f"in the list, or it may have been rejected silently. Check "
                f"`downloads` before assuming it is running."
            ),
            **base,
        }

    row = describe(found)
    summary = (
        f"{name!r} added{via} as {label} -> {save_path} (confirmed, state "
        f"{row['state']}, {row['progress_pct']}%)."
    )
    if row["state"] == "stalledDL" and row["progress_pct"] == 0:
        summary += (
            " It has found no seeders yet. That is normal for the first few "
            "seconds - check `downloads` shortly, and if it has not moved, the "
            "release is dead and another one is the answer."
        )

    return {"ok": True, "confirmed": True, "summary": summary, "torrent": row, **base}


@tool(
    "Start downloading a `.torrent` file directly, bypassing magnet metadata "
    "discovery entirely.\n\n"
    "Use this as a backup path when a magnet from `download` sits stuck at 0% "
    "in `metaDL` or `stalledDL` and never finds peers. Some private trackers "
    "(BTN among them) disable DHT and PEX, so a magnet with no embedded tracker "
    "URL has no way to learn who to ask - it is not slow, it is never going to "
    "resolve. A `.torrent` file carries its own tracker list, and this hands it "
    "to qBittorrent whole rather than reconstructing a magnet from it.\n\n"
    "Pass exactly one of `torrent_url` or `torrent_path`. Filing by kind, the "
    "read-back confirmation, and both library paths all work exactly as in "
    "`download` - this is a different way in, not a different destination.",
    {
        "torrent_url": s("A URL serving the raw .torrent file, fetched "
                          "directly. If it redirects to a magnet: link instead, "
                          "this release never needed this path - use `download` "
                          "with that magnet."),
        "torrent_path": s("Path to a .torrent file already on disk and "
                           "readable by this server (not necessarily by "
                           "whatever is asking for the download)."),
        "kind": s("Override the film/television detection. Only when it is "
                  "visibly about to be wrong.", default="auto",
                  enum=["auto", "movie", "show"]),
    },
)
def download_torrent(torrent_url="", torrent_path="", kind="auto"):
    torrent_url = (torrent_url or "").strip()
    torrent_path = (torrent_path or "").strip()
    if bool(torrent_url) == bool(torrent_path):
        raise ToolError(
            "Pass exactly one of torrent_url or torrent_path - both or neither "
            "were given."
        )

    if not MOVIES_PATH or not SHOWS_PATH:
        raise ToolError(
            "QBT_MOVIES_PATH and QBT_SHOWS_PATH must both be set before anything "
            "can be downloaded, or releases get filed in the wrong library. Set "
            "them in the qbt entry of config.yaml. This is final."
        )

    data = fetch_torrent(torrent_url) if torrent_url else read_torrent_file(torrent_path)

    try:
        infohash, name = torrent_info_hash_and_name(data)
    except ValueError as exc:
        raise ToolError(
            f"That is not a readable .torrent file ({exc}). qBittorrent was not "
            f"touched."
        )

    if kind == "auto":
        kind = detect_kind(name)
    save_path = SHOWS_PATH if kind == "show" else MOVIES_PATH

    result = api_upload("torrents/add", {"savepath": save_path},
                        f"{name}.torrent", data)
    if isinstance(result, str) and result.strip().lower().startswith("fail"):
        raise ToolError(
            f"qBittorrent refused the .torrent file ({result.strip()}). It may "
            f"be corrupt, or the save path {save_path!r} is not writable."
        )

    return _confirm(name, kind, save_path, infohash, via=" from .torrent",
                    extra={"size_bytes": len(data)})


@tool(
    "Report what is downloading. With an infohash, reports that one torrent; "
    "without, reports everything currently active.\n\n"
    "Use this to confirm a download is really progressing rather than assuming "
    "it from the add. A torrent at 0% in `stalledDL` has found nobody and will "
    "not finish - report that plainly instead of calling it downloading.",
    {
        "infohash": s("The infohash returned by `download`. Omit to see "
                      "everything active.", default=""),
        "include_finished": b("Include completed and paused torrents too. Off "
                              "by default - the usual question is what is still "
                              "running.", default=False),
    },
)
def downloads(infohash="", include_finished=False):
    infohash = (infohash or "").strip().lower()

    if infohash:
        rows = api("torrents/info", {"hashes": infohash}) or []
        if not rows:
            raise ToolError(
                f"qBittorrent has no torrent with infohash {infohash}. It was "
                f"never added, or it has since been removed. Do not retry with "
                f"the same hash."
            )
        row = describe(rows[0])
        summary = f"{row['name']!r}: {row['state']}, {row['progress_pct']}%"
        if row["speed_mbps"]:
            summary += f" at {row['speed_mbps']} MB/s"
        if row["eta_minutes"]:
            summary += f", about {row['eta_minutes']} min left"
        summary += "." + stall_note([row])
        return {"ok": True, "summary": summary, "torrents": [row]}

    rows = api("torrents/info") or []
    if not include_finished:
        rows = [r for r in rows if r.get("state") in ACTIVE_STATES]

    out = [describe(r) for r in rows]
    if not out:
        return {
            "ok": True,
            "summary": (
                "Nothing is downloading. Everything has finished, or nothing was "
                "ever added - `qbt_status` will tell you which."
            ),
            "torrents": [],
        }

    return {
        "ok": True,
        "summary": f"{len(out)} torrent(s)." + stall_note(out),
        "torrents": out,
    }


@tool(
    "Remove a torrent. Use it when the wrong thing was grabbed, or when a "
    "release has stalled with no seeders and a different one is going to be "
    "used instead.\n\n"
    "`delete_files` defaults to false, which stops the download but leaves what "
    "arrived on disk. Pass true only when someone has actually said to delete "
    "it - a half-downloaded film is still cheaper to resume than to fetch again, "
    "and this cannot be undone.",
    {
        "infohash": s("The infohash of the torrent to remove."),
        "delete_files": b("Also delete what has downloaded so far. Irreversible.",
                          default=False),
    },
    ["infohash"],
)
def download_cancel(infohash, delete_files=False):
    infohash = (infohash or "").strip().lower()

    rows = api("torrents/info", {"hashes": infohash}) or []
    if not rows:
        raise ToolError(
            f"qBittorrent has no torrent with infohash {infohash}, so there is "
            f"nothing to remove. Check `downloads` for the right hash."
        )
    name = rows[0].get("name")

    api("torrents/delete",
        {"hashes": infohash, "deleteFiles": "true" if delete_files else "false"},
        method="POST")

    # Read back: delete returns an empty body whether or not it did anything.
    still = api("torrents/info", {"hashes": infohash}) or []
    if still:
        raise ToolError(
            f"Asked qBittorrent to remove {name!r} but it is still in the list. "
            f"Report this rather than retrying."
        )

    what = "and deleted the files" if delete_files else "and left the files on disk"
    return {"ok": True, "summary": f"Removed {name!r} {what} (confirmed).",
            "name": name, "infohash": infohash, "files_deleted": delete_files}


def banner():
    return (f"QBT_URL={QBT_URL}  QBT_USER={QBT_USER}  "
            f"QBT_PASS={'set' if QBT_PASS else 'MISSING'}  "
            f"QBT_MOVIES_PATH={MOVIES_PATH or 'MISSING'}  "
            f"QBT_SHOWS_PATH={SHOWS_PATH or 'MISSING'}  "
            f"QBT_CONFIRM_TIMEOUT={CONFIRM_TIMEOUT}s")


if __name__ == "__main__":
    run("qbt-mcp", "1.0", banner)
