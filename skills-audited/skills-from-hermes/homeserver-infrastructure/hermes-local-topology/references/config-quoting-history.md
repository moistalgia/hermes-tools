# Config Quoting — Root Cause and History

## Root cause: cmd.exe eats double quotes

In **cmd.exe**, single quotes are *literal characters*, not quoting. So this:

```cmd
hermes config set terminal.docker_forward_env '["A","B"]'
```

stores the literal string `'[A,B]'` — single quotes kept, double quotes eaten.
Invalid JSON.

**Correct in cmd.exe** (escape the inner double quotes):

```cmd
hermes config set terminal.docker_forward_env "[\"HASS_TOKEN\",\"TAVILY_API_KEY\"]"
```

**Correct in PowerShell** — escape the inner quotes *there too*:

```powershell
hermes config set terminal.docker_forward_env '[\"HASS_TOKEN\",\"TAVILY_API_KEY\"]'
```

⚠️ **PowerShell single-quoting alone is NOT enough.** An earlier version of
this skill said plain `'["A","B"]'` works in PowerShell. Verified false
2026-08-10 — `hermes` goes through a Windows batch shim that re-parses the
argument and eats the double quotes regardless of PowerShell's own quoting.
Only the `\"`-escaped form survives.

## The failure is SILENT — tools go missing, not error

A malformed value does **not** surface as a `ValueError` when you call the
tool. It breaks **toolset assembly at session start**, so `terminal`,
`read_file`, `write_file`, `patch`, `search_files`, and `execute_code` are
simply **absent from the session's tool list**:

```
Tool 'terminal' does not exist. Available tools: browser_back, ...
```

It also **blocks MCP tool injection** into that session — so a newly-added
MCP server's tools go missing at the same time, which looks like two
unrelated bugs. Verified 2026-08-10: one bad `docker_forward_env` value
simultaneously hid the container toolset *and* all 15 `mcp__ha__*` tools;
fixing the JSON and starting a new session restored both at once.

Rule of thumb: for any JSON-valued Hermes config key, escape inner quotes on
Windows regardless of shell. After setting, run one trivial `terminal` call
(`echo ok`) to confirm the value parses before doing real work.
