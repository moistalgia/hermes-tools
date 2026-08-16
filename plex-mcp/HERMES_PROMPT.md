# Prompt for Hermes

The one-time bootstrap prompt for getting `plex-mcp` installed and proven. Once
it works, day-to-day behaviour lives in
[skills/plex-media-playback](../skills/plex-media-playback/SKILL.md) — use that,
not this.

---

Stop iterating on Home Assistant's `media_player.play_media` for Plex — that path is closed. Do not keep guessing at schema variations, and do not write your own plexapi scripts either. I am giving you a finished MCP server for this; your job is to install it and confirm it works, not to design it.

**Install**

1. The server is a git checkout on the Windows host, at `E:/hermes-mcp/hermes-tools/plex-mcp/`. Read it and run it; do not edit it — your edits would be overwritten on the next pull. It installs into its own venv inside the clone: `cd E:/hermes-mcp/hermes-tools/plex-mcp && uv venv && uv pip install -e .`
2. Register it in your MCP config as the server `plex`:

```yaml
mcp_servers:
  plex:
    command: "E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp-serve.exe"
    args: []
    env:
      PLEX_URL: "http://127.0.0.1:32400"
      PLEX_TOKEN: "<token>"
```

`PLEX_TOKEN` is already in your environment — do not search the filesystem for it.

**Verify, in this order, and stop at the first failure**

1. `plex_status` — confirms the connection and lists libraries.
2. `list_players` — gives every player, its `room`, and whether it is controllable. Say the `room` when one is set; otherwise use the `name` verbatim. Do not invent or abbreviate either.
3. `search` with a movie title — confirms the library lookup.
4. `play` with that title and a player name from step 2 — this is the actual test.

Report the output of each step. If a step fails, report the `error` field **exactly as returned** and stop. Do not retry with altered arguments, do not fall back to Home Assistant, and do not write a replacement script. The error text names the cause; I will decide the next move.

**Standing rules**

- Home Assistant handles lights, blinds, thermostats and scenes, through the `hass` server. Never for Plex. Its Plex integration hits the exact same wall this server does and adds nothing.
- Do not call any Home Assistant service that changes state (`stop`, `restart`, `turn_off`, and similar) while testing.
- Do not edit any skill file until I have personally confirmed the playback test worked.
- `list_players` reports every device and whether it is controllable. Read the `status` field before doing anything else:
  - *"registered but not listening"* — the Plex app is closed on that device. Say so and ask me to open it. Do not retry.
  - *"never advertises itself as a player"* — that device can never be a playback target. This is final. Report it verbatim and stop. Do not try `account.resource()`, timeline tricks, raw HTTP, or Home Assistant; all of them have been tested against a live session and all of them fail.
  - *"streaming now, but not registered as a controllable client on this account"* — that device is signed in as a different Plex user. It shows in `now_playing` and can be named, but this token cannot drive it. Report that and stop.
- The Amazon Fire TV in the office is in that second category. Playing something on it manually does not make it controllable — it streams while still refusing every command. Do not treat an active session as evidence that control will work.
