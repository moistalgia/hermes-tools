# plex-mcp

A single-file MCP server for Plex playback. One dependency (`plexapi`), no MCP
SDK, no wrapper framework — the JSON-RPC loop is ~60 lines at the bottom of
[plex_mcp_server.py](plex_mcp_server.py) and you can read all of it.

Every tool is also a CLI subcommand, running through the same dispatch path.
So a human can prove a call works from a shell, and the agent then makes the
identical call over MCP. When something breaks there is exactly one place it
can be breaking.

## Install

Use whatever `python3` / `plexapi` version the container resolves — nothing
here is version-sensitive. Run this **inside the agent container**, since that
is where the server executes:

```bash
pip install -r /opt/hermes-tools/plex-mcp/requirements.txt
```

If pip refuses with `externally-managed-environment`, add
`--break-system-packages`. The dependency installs into the container's own
site-packages — the mount stays read-only and untouched.

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `PLEX_URL` | `http://host.docker.internal:32400` | Use `http://localhost:32400` when testing on the host. |
| `PLEX_TOKEN` | *(required)* | Never read from disk. |
| `PLEX_PROXY` | `1` | Routes player commands through the Plex server instead of dialing the player's LAN IP. Keep on inside Docker. |
| `PLEX_TIMEOUT` | `15` | Seconds. |

## Manual test sequence

Run these in order on the host. Stop at the first one that fails and read the
error — it names the cause.

```bash
export PLEX_URL=http://localhost:32400
export PLEX_TOKEN=your-token-here
```

```bash
python plex_mcp_server.py plex_status
```

```bash
python plex_mcp_server.py list_players
```

```bash
python plex_mcp_server.py search query="ready player one"
```

```bash
python plex_mcp_server.py play query="ready player one" player="EXACT NAME FROM list_players"
```

```bash
python plex_mcp_server.py now_playing
```

Arguments are `key=value`. Quote values containing spaces. Exit code is 0 on
success, 1 on failure, and the JSON body always carries the real error text.

## Wire into Hermes

The path here is the path **inside the container**, not the host path you
cloned to — see [deploying](../README.md#deploying-to-the-media-server). With
the mount from that guide it is `/opt/hermes-tools/plex-mcp/`.

```yaml
mcp_servers:
  plex:
    command: "python3"
    args: ["/opt/hermes-tools/plex-mcp/plex_mcp_server.py", "serve"]
    env:
      PLEX_URL: "http://host.docker.internal:32400"
      PLEX_TOKEN: "your-token-here"
```

Verify the server end without Hermes:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python plex_mcp_server.py serve
```

## Tools

| Tool | Purpose |
| --- | --- |
| `plex_status` | Connection check, libraries, players, session count. Call first when debugging. |
| `list_players` | Controllable players and their exact names. |
| `list_libraries` | Library sections. |
| `search` | Fuzzy title search, returns `rating_key`s. |
| `play` | Search and play the best match. The main one. |
| `play_rating_key` | Play an exact item after a disambiguating search. |
| `play_next_episode` | Next unwatched episode of a show. |
| `control` | play / pause / stop / next / previous. |
| `seek` | Jump to a position. |
| `set_volume` | 0–100, client permitting. |
| `now_playing` | Active sessions, player, state, position. |
| `on_deck` | Continue-watching list. |
| `recently_added` | Newest items. |
| `list_playlists` / `play_playlist` | Playlists, optionally shuffled. |

## Design notes

- **Errors never raise into the protocol.** A failed call returns
  `{"ok": false, "error": "ExceptionType: message"}` plus a hint telling the
  agent to report it verbatim instead of trying argument variations.
- **Player names resolve forgivingly** — exact, then prefix, then substring,
  case-insensitive. Ambiguity and misses return the real list of available
  names, so the agent gets the answer rather than guessing.
- **Search is fuzzy first.** Hub search handles the misspellings that come out
  of speech-to-text; exact per-section title search is the fallback.
- **Asking for a show plays an episode**, not a menu — `play query="the bear"`
  resolves to the next unwatched episode.
- **`PLEX_PROXY` defaults on** because a containerized agent usually cannot
  reach the player's LAN IP directly; the Plex server relays instead.

## Known limitation

`list_players` only shows apps with *Advertise as player* enabled. Some newer
Plex clients (notably recent Apple TV and Plex HTPC builds) never expose the
control API, so they can appear under `currently_streaming_to` in
`list_players` while still being impossible to remote-control. If a device is
missing there, that is a Plex client limitation, not a bug in this server —
no argument variation will fix it.
