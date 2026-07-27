# Prompt for Hermes

Stop iterating on Home Assistant's `media_player.play_media` for Plex — that path is closed. Do not keep guessing at schema variations, and do not write your own plexapi scripts either. I am giving you a finished MCP server for this; your job is to install it and confirm it works, not to design it.

**Install**

1. The file is at `<PATH>/plex_mcp_server.py`. Install its one dependency with whatever works in your container: `pip install plexapi` (add `--break-system-packages` if pip refuses).
2. Register it in your MCP config as the server `plex`:

```yaml
mcp_servers:
  plex:
    command: "python3"
    args: ["<PATH>/plex_mcp_server.py", "serve"]
    env:
      PLEX_URL: "http://host.docker.internal:32400"
      PLEX_TOKEN: "<token>"
```

`PLEX_TOKEN` is already in your environment — do not search the filesystem for it.

**Verify, in this order, and stop at the first failure**

1. `plex_status` — confirms the connection and lists libraries.
2. `list_players` — gives the exact controllable player names. Use those names verbatim from here on; do not invent or abbreviate them.
3. `search` with a movie title — confirms the library lookup.
4. `play` with that title and a player name from step 2 — this is the actual test.

Report the output of each step. If a step fails, report the `error` field **exactly as returned** and stop. Do not retry with altered arguments, do not fall back to Home Assistant, and do not write a replacement script. The error text names the cause; I will decide the next move.

**Standing rules**

- Home Assistant is for lights and blinds only. Never for Plex.
- Do not call any Home Assistant service that changes state (`stop`, `restart`, `turn_off`, and similar) while testing.
- Do not edit any skill file until I have personally confirmed the playback test worked.
- If a device you expect is missing from `list_players`, say so and stop. That is a Plex client limitation and no amount of retrying will fix it.
