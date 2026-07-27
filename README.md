# hermes-tools

Tools for Hermes/GLADYS, the containerized home agent. One directory per tool.

| Tool | What it does |
| --- | --- |
| [plex-mcp](plex-mcp/) | MCP server for Plex playback — search, play, control players. |

## Deploying to the media server

The agent runs in Docker and only sees paths that are mounted into it. So this
repo lives on the **host**, and the container gets a read-only view of it.

Two path spaces are in play and mixing them up is the usual first failure:

- **host path** — where you `git clone` and `git pull`. Example below: `/srv/hermes-tools`.
- **container path** — what the agent puts in its MCP config. Example below: `/opt/hermes-tools`.

### 1. Clone on the host

```bash
git clone https://github.com/moistalgia/hermes-tools.git /srv/hermes-tools
```

### 2. Mount it into the agent, read-only

In the agent's `docker-compose.yml`, under its service:

```yaml
    volumes:
      - /srv/hermes-tools:/opt/hermes-tools:ro
```

```bash
docker compose up -d
```

Read-only is deliberate: the agent can run the tools but cannot dirty the
working tree, commit, or scatter `__pycache__` into it. Python skips writing
bytecode to a non-writable directory and runs normally.

If you would rather reuse a mount the container already has (e.g. the
`hermes-agent/in` share), clone into a **subdirectory** of it — say
`hermes-agent/in/tools/hermes-tools` — not the root. An inbox directory that
something scans or drains is not a safe home for a `.git`.

### 3. Confirm the container can see it

```bash
docker exec <agent-container> ls /opt/hermes-tools/plex-mcp
```

That path — the one that just worked — is what goes in the MCP config. Then
follow [plex-mcp/README.md](plex-mcp/README.md) to install and register it.

## Updating

Always on the host, never in the container (it has no git, no credentials, and
a `:ro` mount is unwritable anyway):

```bash
git -C /srv/hermes-tools pull
```

A bind mount is live, so the container sees new files immediately. A changed
`.py` still needs the agent to restart its MCP servers to load it.
