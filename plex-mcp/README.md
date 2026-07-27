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
pip install -r /sandbox/in/tools/plex-mcp/requirements.txt
```

If pip refuses with `externally-managed-environment`, add
`--break-system-packages`. The dependency installs into the container's own
site-packages; nothing is written to the mount.

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `PLEX_URL` | `http://host.docker.internal:32400` | Correct when the container runs on the same host as Plex. If Plex lives elsewhere, give its LAN address. Use `http://localhost:32400` when testing on the Plex host itself. |
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
the mount from that guide it is `/sandbox/in/tools/plex-mcp/`.

```yaml
mcp_servers:
  plex:
    command: "python3"
    args: ["/sandbox/in/tools/plex-mcp/plex_mcp_server.py", "serve"]
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
| `list_players` | Every known player and whether it can be driven right now. Does not require playback. |
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
  then product/platform, case-insensitive. The last pass matters: Fire TV
  registers under the name `unknown`, so "fire" has to reach it somehow.
  Naming a device that exists but cannot be driven returns *why*, not "no
  match", so the agent reports the cause instead of hunting for a workaround.
- **Search is fuzzy first.** Hub search handles the misspellings that come out
  of speech-to-text; exact per-section title search is the fallback.
- **Asking for a show plays an episode**, not a menu — `play query="the bear"`
  resolves to the next unwatched episode.
- **The command route is chosen per device.** Players registered with the
  server are driven through it; players known only to plex.tv are dialed
  directly on the LAN address it advertises, but only after a probe confirms
  something is listening. `PLEX_PROXY` remains the default for the former.

## How players are discovered

Three endpoints disagree about what a "player" is, and reading only the first
is why an idle device used to look like it did not exist:

| Source | Contains | Survives idle |
| --- | --- | --- |
| `/clients` on the PMS | Companion registrations with *this server* | no |
| `plex.tv/devices.xml` | everything on the account, plus LAN address | **yes** |
| `/status/sessions` | what is streaming now | n/a — says nothing about control |

`list_players` merges all three and reports, per device, whether it can be
driven *right now* and why not. Playback is not required for a device to
appear. Each entry carries a `route`:

- `server` — the device is in `/clients`; commands are relayed by the PMS.
- `direct` — not in `/clients`, but its Companion listener answered on the LAN
  address plex.tv advertises, so commands go straight there.

Browser tabs and controller-only apps (Home Assistant, the Plex web UI) are
filtered out unless they are streaming; pass `include_all=true` to see them.

## Known limitation: some clients can never be controlled

A device whose plex.tv `provides` field omits `player` cannot be a playback
target, no matter what it is doing. **Amazon Fire TV is the case that matters
here** — the Kepler-based app advertises nothing at all.

This was verified against a live Fire TV session, mid-playback:

- `/clients` → empty
- `protocolCapabilities` on the session → `['']`
- a relayed read-only `timeline/poll` → **404 not_found**
- Home Assistant's own Plex integration → `HomeAssistantError: Client is not
  currently accepting playback controls`

There is no argument variation, alternate endpoint, or `account.resource()`
trick that changes this — `account.resources()` does not list the Fire TV at
all. `list_players` reports such devices with `controllable: false` and a
reason. **Report the reason and stop.**

The only remote-control path to a Fire TV is ADB (Home Assistant's `androidtv`
integration), which is outside this server's scope.

A device that advertises `player` but is not listening simply has its Plex app
closed. Open Plex on it and retry — Roku only listens on `:8324` while the app
is open.
