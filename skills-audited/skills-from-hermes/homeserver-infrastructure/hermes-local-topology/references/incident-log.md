# Incident Log

## Leaked credentials via a Windows-style path (cleaned up 2026-08-10)

A past session ran `echo x > "C:\root\.plex.env"` inside Linux. Backslashes
are legal filename characters in POSIX, so that created a *single file
literally named* `C:\root\.plex.env` — leaking credentials in plaintext.

To find/remove such entries (shell quoting is painful — let `find` do it):

```bash
cd /root && find . -maxdepth 1 -name 'C:*' -exec rm -rf {} +
```

This is why the hot-path rule is "never use Windows-style paths in container
commands — POSIX only."

## A wedged container reported Home Assistant as "down"

HA was declared "down" off `http=000` responses from a wedged container. HA
was fine the whole time — a poisoned 9p session returns empty for all
network probes and may report a Windows `cwd`. Trust the user's lived
experience over a single failed probe; sanity-check the container itself
first (`echo ALIVE`) before concluding a service is unreachable.

## MCP toolset vanishing mid-session

After adding the `ha` server and restarting the gateway, `mcp__ha__*` still
did not resolve in the open session (`hermes gateway restart` proved
insufficient — verified 2026-08-10). Worse, in the same episode the entire
`terminal` toolset vanished mid-session (`terminal`, `read_file`,
`write_file`, `patch`, `search_files`, `execute_code` all gone —
`Tool 'terminal' does not exist`), leaving only host-side tools. Root cause
was a malformed `docker_forward_env` JSON value (see
`config-quoting-history.md`) — but the practical lesson generalizes: after
any MCP or toolset change, always start a new chat session before testing,
and if tools go missing mid-session, suspect this class of issue before
suspecting a broken server.
