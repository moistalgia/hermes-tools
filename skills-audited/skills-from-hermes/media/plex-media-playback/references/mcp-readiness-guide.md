# Plex MCP readiness & diagnosis

## Architecture (corrected Aug 2026)

The Plex MCP server runs **host-side on Windows**, spawned by Hermes from
`~/.hermes/config.yaml`. It is **not** in the Docker sandbox container.

```
E:/hermes-mcp/hermes-tools/plex-mcp/.venv/Scripts/plex-mcp-serve.exe
```

Because it runs on the host, it talks to Plex at `http://127.0.0.1:32400`
directly. It does not need and must not use `host.docker.internal`.

> **Superseded guidance.** An earlier version of this file described a "CLI
> fallback" at `/sandbox/in/hermes-tools/plex-mcp/plex_mcp_server.py` inside the
> container, and embedded a plaintext `PLEX_TOKEN`. Both were wrong. MCPs are
> never invoked from inside the container, and secrets never belong in a skill
> file. That token has been removed here and should be rotated in Plex.

## Correct tool naming

Tools are exposed to the agent as `mcp__plex__<tool>` — **double** underscores.

```
mcp__plex__play          ✅
mcp_plex_play            ❌
play                     ❌
```

If a bare name like `play` doesn't resolve, that is expected — it was never the
real name. Use the `mcp__plex__*` form.

## Diagnosing "the Plex tools aren't there"

1. **Confirm the tool exists**
   `tool_search(query="plex playback")` — the 15 `mcp__plex__*` tools should
   appear.

2. **If nothing appears**, the MCP server isn't loaded. Causes, in order:
   - Hermes wasn't restarted after editing `config.yaml` (config reads at boot)
   - The `.exe` path in `config.yaml` is wrong — verify the file exists
   - YAML indentation under `mcp_servers:` is malformed
   - The server crashes on startup — run the `.exe` manually on the host; a
     stdio server should block and wait, not exit

3. **If tools exist but calls fail**, run `mcp__plex__plex_status` first. It
   separates "server unreachable" from "token rejected" from "bad arguments".

## What NOT to do

Do not fall back to Home Assistant, raw `curl`, or a hand-written Python script
when a Plex tool fails. Those are verified dead ends — see
[troubleshooting.md](troubleshooting.md). If the MCP is down, the correct action
is to report that it's down and why, not to improvise a transport.

There is no in-container fallback path. If the MCP server is not loaded, the fix
is host-side configuration, not a different transport.
