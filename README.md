# hermes-tools

Tools for Hermes/GLADYS, the containerized home agent. One directory per tool.

| Tool | What it does |
| --- | --- |
| [plex-mcp](plex-mcp/) | MCP server for Plex playback — search, play, control players, recommend. |
| [skills/plex-media-playback](skills/plex-media-playback/) | Hermes skill that drives the above. |

## Deploying to the media server

The agent runs in Docker and only sees paths mounted into it. It already has one:

| | |
| --- | --- |
| host | `<somewhere>/hermes-agent/in` |
| container | `/sandbox/in` |

Two path spaces, and mixing them up is the usual first failure. You `git pull`
on the **host** path; the agent's MCP config must name the **container** path.

### Clone into the existing mount

Clone so the repo contents land in a `tools` subdirectory, not at the root of
`in` — an inbox that something scans or drains is not a safe home for a `.git`:

```bash
git clone https://github.com/moistalgia/hermes-tools.git <host>/hermes-agent/in/tools
```

The agent then sees `/sandbox/in/tools/plex-mcp/plex_mcp_server.py`, which is
the path that goes in its MCP config.

**A loose copy of `plex-mcp/` is not enough.** It cannot `git pull`, so it
silently stays on whatever revision was copied. If `/sandbox/in/plex-mcp`
already exists as a plain directory, delete it after the clone so there is only
one copy and no ambiguity about which one the agent loaded.

### Confirm the container can see it

```bash
docker exec <agent-container> ls /sandbox/in/tools/plex-mcp
```

The path that just worked is the one to configure. Then follow
[plex-mcp/README.md](plex-mcp/README.md) to install and register it.

### Optional: make it read-only

The `in` mount is presumably writable. If you later give the repo its own mount,
add `:ro` — the agent can still run the tools, but cannot dirty the working
tree, commit, or scatter `__pycache__` into it. Python skips writing bytecode to
a non-writable directory and runs normally.

## Updating

Always on the host, never in the container — it has no git and no credentials:

```bash
git -C <host>/hermes-agent/in/tools pull
```

A bind mount is live, so the container sees new files immediately. A changed
`.py` still needs the agent to restart its MCP servers to load it.

## If you move the repo

The container path appears in exactly two places. Change both:

- [plex-mcp/README.md](plex-mcp/README.md) — the `mcp_servers` block
- [plex-mcp/HERMES_PROMPT.md](plex-mcp/HERMES_PROMPT.md) — the install step

Nothing in the Python reads an install path, so no code changes.
