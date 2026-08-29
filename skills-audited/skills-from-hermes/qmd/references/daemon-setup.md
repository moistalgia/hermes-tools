# QMD — MCP Setup and Daemon Mode

qmd exposes an MCP server that provides search tools directly to Hermes Agent
via the native MCP client. This is the preferred integration — once
configured, the agent gets qmd tools automatically without needing to load
the `qmd` skill.

## Option A: Stdio Mode (Simple)

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  qmd:
    command: "qmd"
    args: ["mcp"]
    timeout: 30
    connect_timeout: 45
```

This registers tools: `mcp_qmd_search`, `mcp_qmd_vsearch`,
`mcp_qmd_deep_search`, `mcp_qmd_get`, `mcp_qmd_status`.

**Tradeoff:** Models load on first search call (~19s cold start), then stay
warm for the session. Acceptable for occasional use.

## Option B: HTTP Daemon Mode (Fast, Recommended for Heavy Use)

Start the qmd daemon separately — it keeps models warm in memory:

```bash
# Start daemon (persists across agent restarts)
qmd mcp --http --daemon

# Runs on http://localhost:8181 by default
```

Then configure Hermes Agent to connect via HTTP:

```yaml
mcp_servers:
  qmd:
    url: "http://localhost:8181/mcp"
    timeout: 30
```

**Tradeoff:** Uses ~2GB RAM while running, but every query is fast (~2-3s).
Best for users who search frequently.

## Keeping the Daemon Running

### macOS (launchd)

```bash
cat > ~/Library/LaunchAgents/com.qmd.daemon.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.qmd.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>qmd</string>
    <string>mcp</string>
    <string>--http</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/qmd-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/qmd-daemon.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.qmd.daemon.plist
```

### Linux (systemd user service)

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/qmd-daemon.service << 'EOF'
[Unit]
Description=QMD MCP Daemon
After=network.target

[Service]
ExecStart=qmd mcp --http --daemon
Restart=on-failure
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now qmd-daemon
systemctl --user status qmd-daemon
```

## Recommendations

- **Prefer MCP integration** — once configured, the agent gets native tools
  without needing to load this skill each time.
- **Daemon mode for frequent users** — if the user searches their knowledge
  base regularly, recommend the HTTP daemon setup over stdio mode.
