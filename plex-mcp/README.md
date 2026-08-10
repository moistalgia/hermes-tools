# plex-mcp

A single-file MCP server for Plex playback. One dependency (`plexapi`), no MCP
SDK, no wrapper framework — the JSON-RPC loop is ~60 lines at the bottom of
[plex_mcp_server.py](plex_mcp_server.py) and you can read all of it.

Every tool is also a CLI subcommand, running through the same dispatch path.
So a human can prove a call works from a shell, and the agent then makes the
identical call over MCP. When something breaks there is exactly one place it
can be breaking.

## Install

This server runs on the Windows host, from its own venv inside the clone. One
directory, one path to remember:

```bash
git clone https://github.com/moistalgia/hermes-tools.git E:/hermes-mcp/hermes-tools
```

```bash
cd E:/hermes-mcp/hermes-tools/plex-mcp && uv venv && uv pip install -e .
```

Because the install is editable, `git pull` updates the running server — no
reinstall, just restart the MCP connection. `.venv/` is gitignored, so it will
not dirty the tree.

That installs `plexapi` and two entry points into `.venv\Scripts\`:

| Entry point | Use |
| --- | --- |
| `plex-mcp-serve.exe` | **the MCP server.** Goes straight to the stdio loop, takes no arguments. |
| `plex-mcp.exe` | the CLI, for proving things work from a shell. |

`plex-mcp` is the only server here with a dependency. The other three are
standard library only and run straight from the checkout — see the
[root README](../README.md#deploying).

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `PLEX_URL` | `http://127.0.0.1:32400` | Correct when Plex runs on the same host as Hermes. If Plex lives elsewhere, give its LAN address. |
| `PLEX_BASEURL` | — | Accepted as an alias for `PLEX_URL`, since that is plexapi's name and what most MCP config examples use. `PLEX_URL` wins if both are set. |
| `PLEX_TOKEN` | *(required)* | Never read from disk. |
| `PLEX_PROXY` | `1` | Routes player commands through the Plex server instead of dialing the player's LAN IP. Leave it on unless a device is only reachable directly. |
| `PLEX_TIMEOUT` | `15` | Seconds. |
| `PLEX_ALIASES` | `{}` | JSON map of spoken names to Plex player names, e.g. `{"theater":"Streaming Stick 4K","office":"unknown"}`. Rooms outlive hardware; Plex names like `unknown` are unsayable. |
| `ROKU_PLEX_CHANNEL_ID` | `13535` | Roku channel ID for Plex, used to wake the app. |

## Manual test sequence

Run these in order. Stop at the first one that fails and read the error — it
names the cause.

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe plex_status
```

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe list_players
```

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe search query="ready player one"
```

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe play query="ready player one" player="EXACT NAME FROM list_players"
```

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe now_playing
```

Arguments are `key=value`. Quote values containing spaces. Exit code is 0 on
success, 1 on failure, and the JSON body always carries the real error text.

## Tools

| Tool | Purpose |
| --- | --- |
| `plex_status` | Connection check, libraries, players, session count. Call first when debugging. |
| `list_players` | Every known player and whether it can be driven right now. Does not require playback. |
| `list_libraries` | Library sections. |
| `library_overview` | Section sizes plus the exact genre/decade/rating vocabulary. Call before `discover`. |
| `discover` | Filtered browse — genre, decade, rating, unwatched, actor, director. The recommendation workhorse. |
| `similar_to` | "Something like X", ranked by shared genres. |
| `search` | Fuzzy title search, returns `rating_key`s. |
| `play` | Search and play the best match. The main one. |
| `play_rating_key` | Play an exact item after a disambiguating search. |
| `play_next_episode` | Next unwatched episode of a show. |
| `control` | play / pause / stop / next / previous. `stop` works on any streaming device, including ones that refuse all other commands. |
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

## Wire into Hermes

Hermes reads `%USERPROFILE%\.hermes\config.yaml`:

```yaml
mcp_servers:
  plex:
    command: "E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp-serve.exe"
    args: []
    env:
      PLEX_URL: "http://127.0.0.1:32400"
      PLEX_TOKEN: "***"
      PLEX_ALIASES: '{"theater":"Streaming Stick 4K","office":"unknown"}'
    timeout: 60
    connect_timeout: 30
    tools:
      include: [plex_status, list_players, play, play_rating_key,
                play_next_episode, control, now_playing, search,
                library_overview, discover, similar_to, on_deck,
                recently_added, seek, set_volume]
      resources: false
      prompts: false
```

Things that will silently break it:

- **Use the real tool names.** `tools.include` is an allowlist — names that do
  not exist expose nothing. There is no `search_media`, `play_on_client` or
  `list_clients` here; see the tool table above.
- **Keep the server named `plex`.** Tools register as `mcp_plex_play`; hyphens
  and dots become underscores, so `plex-mcp` would give you `mcp_plex_mcp_play`.
- **`python.exe`, never `pythonw.exe`.** Windowless Python has no usable
  stdin/stdout and the handshake hangs forever. Using the entry point avoids the
  question entirely.
- **Env must be explicit.** stdio servers get only the configured `env` plus a
  safe baseline, so `PLEX_TOKEN` is not inherited from a shell or `.env` file.
- Absolute path, forward slashes. Hermes spawns the process directly, not
  through a shell, so there is no PATH lookup and no venv activation.

Nothing in this server deletes, refreshes or restarts anything, so the allowlist
is about keeping the tool list small rather than fencing off destructive verbs.

### Verify before letting the agent near it

```bash
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp.exe plex_status
```

Then the handshake, which should return exactly two lines of JSON and nothing
else:

```bash
printf '%s
' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp-serve.exe
```

Any non-JSON on stdout means something is printing where it should not. The
server redirects `sys.stdout` to stderr for the lifetime of the stdio loop
specifically to prevent this, so a stray line points at something writing before
the loop starts.

Then `hermes dashboard` → MCP → Test, and `/reload-mcp` in a session. Edit
`config.yaml` from a separate terminal, not mid-conversation — a running session
auto-reloads MCP connections on change with a 30s timeout.

## Waking a sleeping Roku

Roku exposes ECP on port `8060` with no auth, and answers from the home screen.
The Plex Companion listener on `:8324` only runs while the Plex app is open, so
a device that is on but idle used to be a dead end. Now `play` detects that
case, `POST`s `/launch/13535` to open Plex, waits for `:8324` to come up, and
proceeds. Cold start to playing is roughly 15 seconds.

If the Roku does not answer ECP at all it is powered off, and the tool says so
rather than retrying.

## Stopping what cannot be controlled

`control action=stop` first tries `/status/sessions/terminate`, which is a
**server** operation and never touches Companion. It therefore stops playback on
devices that reject every other command — Amazon Fire TV included. Pause, seek
and volume still require Companion and remain unavailable there.

## Recommendations

`discover` is the tool for open-ended requests. Two things about Plex filtering
that are easy to get wrong, both handled here:

- **Multiple genres default to AND.** Plex joins a list with `,` (OR); repeating
  the parameter is AND. A "fantasy epic" is Fantasy *and* Adventure, so
  `match_all` defaults true. Set it false to widen.
- **Listing endpoints truncate tag lists to two entries.** A search result will
  claim a Fantasy film has genres `['Comedy', 'Animation']`. Anything reporting
  genres reloads the item first, or it misinforms the agent.

`similar_to` ranks by shared-genre overlap rather than Plex's `similar` field,
which is empty on this server.
