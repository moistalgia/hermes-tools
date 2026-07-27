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
    PLEX_URL    default http://host.docker.internal:32400
    PLEX_TOKEN  required
    PLEX_PROXY  default 1 - route player commands through the Plex server
                instead of connecting to the player's LAN IP directly.
                Keep this on when running inside Docker.
"""

import inspect
import json
import os
import sys
import traceback

PLEX_URL = os.environ.get("PLEX_URL", "http://host.docker.internal:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_PROXY = os.environ.get("PLEX_PROXY", "1") not in ("0", "false", "False", "")
PLEX_TIMEOUT = int(os.environ.get("PLEX_TIMEOUT", "15"))

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "plex"
SERVER_VERSION = "1.0.0"


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


def ms_to_clock(ms):
    if not ms:
        return "0:00"
    total = int(ms // 1000)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def describe_item(item):
    """Flatten a Plex media object into something an LLM can reason about."""
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
    summary = getattr(item, "summary", None)
    if summary:
        out["summary"] = summary[:300]
    return out


def describe_player(client):
    return {
        "name": getattr(client, "title", None),
        "product": getattr(client, "product", None),
        "device": getattr(client, "device", None),
        "platform": getattr(client, "platform", None),
        "address": getattr(client, "address", None),
        "machine_identifier": getattr(client, "machineIdentifier", None),
        "capabilities": getattr(client, "protocolCapabilities", None),
    }


# ---------------------------------------------------------------------------
# Resolution helpers - the parts that usually cause the "why did it not play"
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """An error with a message meant to be shown verbatim to the agent."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


def resolve_player(name):
    """Find a client by exact, prefix, then substring match (case-insensitive).

    Returns the PlexClient, ready to receive commands. Raises ToolError listing
    the real available names rather than letting the agent guess.
    """
    clients = plex().clients()
    if not clients:
        raise ToolError(
            "Plex reports zero controllable players. The target app must be open "
            "and have 'Advertise as player' enabled; some newer Plex clients "
            "never expose the control API at all."
        )
    names = [c.title for c in clients]
    if not name:
        if len(clients) == 1:
            chosen = clients[0]
        else:
            raise ToolError(
                "No player specified and more than one is available.",
                available_players=names,
            )
    else:
        want = name.strip().lower()
        matches = [c for c in clients if c.title.lower() == want]
        if not matches:
            matches = [c for c in clients if c.title.lower().startswith(want)]
        if not matches:
            matches = [c for c in clients if want in c.title.lower()]
        if not matches:
            raise ToolError(
                f"No player matches {name!r}.", available_players=names
            )
        if len(matches) > 1:
            raise ToolError(
                f"{name!r} is ambiguous.",
                candidates=[c.title for c in matches],
            )
        chosen = matches[0]

    if PLEX_PROXY:
        # Route commands via the Plex server rather than dialing the player's
        # LAN IP. Required whenever this process is network-isolated (Docker).
        chosen.proxyThroughServer(True)
    return chosen


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
    players = [c.title for c in p.clients()]
    return {
        "ok": True,
        "server": p.friendlyName,
        "version": p.version,
        "url": PLEX_URL,
        "proxy_through_server": PLEX_PROXY,
        "libraries": sections,
        "players": players,
        "active_sessions": len(p.sessions()),
    }


@tool("List Plex players that can be controlled, with their exact names. Use the 'name' value verbatim as the 'player' argument elsewhere.")
def list_players():
    p = plex()
    players = [describe_player(c) for c in p.clients()]
    playing_on = []
    for session in p.sessions():
        for player in getattr(session, "players", []) or []:
            playing_on.append(
                {"name": player.title, "product": player.product, "state": player.state}
            )
    return {
        "ok": True,
        "players": players,
        "count": len(players),
        "currently_streaming_to": playing_on,
        "note": (
            "Only apps with 'Advertise as player' enabled appear here. A device "
            "that shows under currently_streaming_to but not under players cannot "
            "be remote-controlled by this API."
        ),
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
    return {
        "ok": True,
        "action": "playing",
        "player": client.title,
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
        "action": s("One of: play, pause, stop, next, previous."),
        "player": s("Player name from list_players."),
    },
    ["action"],
)
def control(action, player=None):
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
    }
    key = action.strip().lower()
    if key not in actions:
        raise ToolError(
            f"Unknown action {action!r}.", valid_actions=sorted(set(actions))
        )
    actions[key](mtype="video")
    return {"ok": True, "action": key, "player": client.title}


@tool(
    "Jump to a position in whatever is currently playing.",
    {
        "seconds": i("Absolute position in seconds from the start."),
        "player": s("Player name from list_players."),
    },
    ["seconds"],
)
def seek(seconds, player=None):
    client = resolve_player(player)
    client.seekTo(int(seconds) * 1000, mtype="video")
    return {
        "ok": True,
        "player": client.title,
        "position": ms_to_clock(int(seconds) * 1000),
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


def serve():
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
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


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
