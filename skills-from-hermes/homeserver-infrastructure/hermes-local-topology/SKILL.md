---
name: hermes-local-topology
description: "Where this Hermes runs: host vs container, paths, ports."
version: 1.0.0
author: gladys + moisty
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [infrastructure, docker, wsl2, topology, filesystem, homeserver]
    related_skills: [mcp-authoring, web-access, hermes-agent]
---

# This Machine's Hermes Topology

## When to Use

Load when a task involves **where code runs, where files land, reaching a
service by host/port, missing dependencies, or a wedged terminal**. Read this
before debugging anything that smells like "it works here but not there."

## The split that explains almost everything

**Hermes core runs natively on Windows. Only shell/file tools run in Docker.**

| Runs on the Windows host | Runs in the Docker sandbox |
| --- | --- |
| `web_search`, `web_extract` | `terminal` |
| `browser_*` | `read_file`, `write_file`, `patch` |
| MCP servers (Plex, future ones) | `search_files` |
| `skill_manage`, `cronjob`, memory | `execute_code` |

Verified: `web_extract` caches to
`C:\Users\Nick\AppData\Local\hermes\cache\web\`, and `skill_manage` writes
succeed while the container sees `/root/.hermes/skills` as read-only.

### Consequences you must respect

- **From inside the container**, a host service is `host.docker.internal`, never
  `127.0.0.1`.
- **From an MCP server** (host-side), the same service *is* `127.0.0.1`.
- **Host-side agent tools (`browser_navigate`, `web_extract`, `vision_analyze`) refuse
  localhost/private/internal URLs by design** — an SSRF guard, not a bug. Verified
  2026-08-16 across all three. Don't try to get it loosened for routine local-dev
  checks; do local-server verification from **inside the container** instead
  (`curl` against `127.0.0.4:<port>`, or the workflow in
  `webapp-predeploy-verification` Gate 3/4).
- **Never** reference `/sandbox/...` paths in MCP code or host-side config.
- Deleting the container kills `terminal` / `read_file` / `execute_code` for the
  session while `web_search`, MCPs, and `skill_manage` keep working.

## The container is Hermes-managed

`terminal.backend: docker`. Hermes spawns **one long-lived container** reused
across tool calls, `/new`, and subagents for the life of the Hermes process.

**Do not `docker run` or `docker compose` a container for this** — that creates
an unrelated container Hermes ignores.

**Restarting the desktop app does NOT reliably recycle the container.** Verified:
after a full app restart plus `hermes gateway restart`, the same container from
before the config change was still in use. So `docker_forward_env` /
`docker_image` changes may not take effect.

Check the container's age against when you changed config:

```bash
stat -c %y /proc/1     # container start time
date                   # now
```

If the container predates your config change, the change hasn't applied. Force a
new one by stopping the container host-side (`docker stop <id>`) — Hermes
provisions a fresh one on the next terminal call — or fully exit every Hermes
process first.

Configure it via `hermes config set` (never hand-edit `config.yaml`):

| Key | Purpose |
| --- | --- |
| `terminal.docker_image` | Base image — bake in deps here |
| `terminal.docker_volumes` | Host↔container mounts (`-v` syntax) |
| `terminal.docker_forward_env` | Forward host env vars (JSON array — see quoting trap) |
| `terminal.docker_env` | Literal `KEY=value` injections |
| `terminal.container_persistent` | Persist container FS across sessions |

`~/.hermes/skills` is auto-mounted **read-only** by design. Edit skills with
`skill_manage`, never with file writes.

## Filesystem map

```
/sandbox   → E:\ subpath bind (only in/out/temp)   persistent, user-browsable
/root      → sandbox-local home                    NOT the C: drive
/tmp, /    → overlay                               fast, ephemeral
```

Real directories: `/sandbox/in`, `/sandbox/out`, `/sandbox/temp`.
**There is no `/sandbox/tmp`.**

Verified: `/root` contains only `.cache`, `.config`, `.hermes` — no `Windows`,
`Users`, or `Program Files`. Earlier notes claiming `/root` *is* `C:\` were
wrong. The host MCP directory `C:\dev\hermes-tools\` is **not reachable** from
the container, which is correct isolation.

### ⚠️ Never use Windows-style paths in container commands

A past session ran `echo x > "C:\root\.plex.env"` inside Linux. Backslashes are
legal filename characters in POSIX, so that created a *single file literally
named* `C:\root\.plex.env` — leaking credentials in plaintext. Cleaned up
2026-08-10.

To find/remove such entries (shell quoting is painful — let `find` do it):

```bash
cd /root && find . -maxdepth 1 -name 'C:*' -exec rm -rf {} +
```

Rules:
- POSIX paths only inside the container.
- Scratch work in `/tmp`, deliverables in `/sandbox/out`.
- Secrets never in a file on a mount — use host-side secret storage plus
  `terminal.docker_forward_env`.

### ⚠️ 9p is slow and will wedge your session

Recursive `grep`/`find` across `/sandbox` or `/root/.hermes` **hangs
indefinitely** and can poison the whole terminal session — after which even
`echo` times out and `cwd` may report a Windows path.

**The working pattern:**

```bash
mkdir -p /tmp/work && cd /tmp/work   # do everything on fast overlay
# ... build, extract, iterate ...
cp final.xlsx /sandbox/out/          # one 9p write at the end
```

Rules:
- Never recursive-walk a 9p mount. Target exact paths.
- Wrap 9p touches in `timeout 10 ...` so a stall fails fast.
- Prefer `read_file <exact path>` over shell tree walks.

## Container dependency reality

The base image ships Python 3.11 + Node 20 and **almost nothing else**. Absent
by default: `pandas`, `numpy`, `openpyxl`, `python-docx`, `python-pptx`, `PIL`,
`requests`, `pyyaml`, `matplotlib` — and the binaries `jq`, `gh`, `ffmpeg`,
`pandoc`, `pygount`.

`pip install` works (network is fine), but it's per-session tax. Skills that
assume `gh` (all `github-*`), `jq` (`gif-search`), or `ffmpeg`
(`ascii-video`, `songsee`) **will fail until installed**.

Permanent fix: point `terminal.docker_image` at a custom image with those baked
in.

## Ports (verified from the container)

| Target | Result |
| --- | --- |
| `host.docker.internal:32400` | ✅ Plex, HTTP 401 (auth required — correct) |
| `host.docker.internal:8123` | ✅ Home Assistant, HTTP 200; `/api/` → 401 (needs token) |
| `host.docker.internal:8080` | ✅ some HTTP service, 200 |
| `11434`, `8000`, `1234`, `30000` | ❌ closed — no local LLM server running |

### ⚠️ New MCP tools require a NEW CHAT SESSION

A running session's toolset is fixed at session start. After
`hermes mcp add ...`, the new tools do **not** appear in the current chat — the
CLI even prints *"Start a new session to use these tools."*

`hermes gateway restart` is **not** sufficient. Verified 2026-08-10: after
adding the `ha` server and restarting the gateway, `mcp__ha__*` still did not
resolve in the open session.

Worse, a gateway restart can **silently drop toolsets** from the running
session. In the same episode the entire `terminal` toolset vanished mid-session
(`terminal`, `read_file`, `write_file`, `patch`, `search_files`,
`execute_code` all gone — `Tool 'terminal' does not exist`), leaving only
host-side tools.

**Rule:** after any MCP or toolset change, start a new chat session before
testing. If tools are missing mid-session, that is the first thing to suspect —
not a broken server.

Note `tool_search` only reports *deferrable* tools; directly-loaded tools may be
absent from its results while still being callable. To check whether a tool is
truly present, attempt the call — an "is not a deferrable tool" error means the
name is wrong or the tool is not loaded at all.

## ⚠️ A malformed config value kills ALL container tools

`terminal.docker_forward_env` (and friends) are parsed as **strict JSON** at the
start of *every* terminal call. A malformed value takes down `terminal`,
`read_file`, `write_file`, `patch`, `search_files`, and `execute_code`
simultaneously, with:

```
ValueError: Invalid value for TERMINAL_DOCKER_FORWARD_ENV:
  "'[HASS_TOKEN,TAVILY_API_KEY]'" (expected valid JSON)
```

Host-side tools (`skill_manage`, `browser_*`, `web_search`, MCPs, memory) keep
working, which makes the failure look narrower than it is.

### Root cause: cmd.exe eats double quotes

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

⚠️ **PowerShell single-quoting alone is NOT enough.** An earlier version of this
skill said plain `'["A","B"]'` works in PowerShell. Verified false 2026-08-10 —
`hermes` goes through a Windows batch shim that re-parses the argument and eats
the double quotes regardless of PowerShell's own quoting. Only the `\"`-escaped
form survives.

**How to tell if the value is mangled** — run `hermes config get` and look at
the echo:

| Echo | Meaning |
| --- | --- |
| `["HASS_TOKEN","TAVILY_API_KEY"]` | ✅ valid JSON |
| `[HASS_TOKEN,TAVILY_API_KEY]` | ❌ quotes eaten — broken |
| `'[HASS_TOKEN,TAVILY_API_KEY]'` | ❌ literal string — broken |

Bare unquoted names inside brackets is the tell. Fix it before anything else.

### The failure is SILENT — tools go missing, not error

A malformed value does **not** surface as a `ValueError` when you call the tool.
It breaks **toolset assembly at session start**, so `terminal`, `read_file`,
`write_file`, `patch`, `search_files`, and `execute_code` are simply **absent
from the session's tool list**:

```
Tool 'terminal' does not exist. Available tools: browser_back, ...
```

It also **blocks MCP tool injection** into that session — so a newly-added MCP
server's tools go missing at the same time, which looks like two unrelated bugs.
Verified 2026-08-10: one bad `docker_forward_env` value simultaneously hid the
container toolset *and* all 15 `mcp__ha__*` tools; fixing the JSON and starting a
new session restored both at once.

**Diagnostic that settles it in 15 seconds:** spawn a subagent and have it call
`terminal`. A child's toolset is built fresh at spawn time, so:

- Child **has** `terminal` → config is now correct; your session's toolset is
  just stale. `/new` fixes it. No app restart needed.
- Child **lacks** it too → the config value is still broken.

Rule of thumb: for any JSON-valued Hermes config key, escape inner quotes on
Windows regardless of shell. After setting, run one trivial `terminal` call
(`echo ok`) to confirm the value parses before doing real work.

## ⚠️ A wedged container reports everything as dead

If probes return `http=000` for *every* target, **suspect the container before
concluding the services are down.** A poisoned 9p session returns empty for all
network probes, and may report a Windows `cwd`.

Sanity-check first:

```bash
echo ALIVE    # if this times out, the session is dead, not the network
```

This actually happened: HA was declared "down" off `000` responses from a wedged
container. HA was fine the whole time. Trust the user's lived experience over a
single failed probe.

## Skill platform gating

Skills are filtered by their `platforms:` frontmatter against the **Windows
host**, not the Linux container. So `platforms: [linux, macos]` skills are
**invisible here** and `skill_view` returns
`"not supported on this platform"`.

That is the mechanism behind the 89-on-disk vs 77-indexed gap — not a bug. To
use such a skill, add `windows` to its `platforms` list (only if it genuinely
works host-side).

## Known tool bug

`search_files` silently returns **0 matches** for any path with a
dot-directory ancestor (e.g. `/root/.hermes/skills`). Filesystem-independent,
reproduced on both overlay and 9p. It fails *silently* — looks like a clean
"no results."

Workaround: `grep` with an exact path via `terminal`. Never trust a
zero-result `search_files` under a dot-directory.

## Not currently wired

- **No SSH keys / `.gitconfig`** in the container → cannot push to repos yet.
  Fix via `terminal.docker_volumes` mounting the key read-only plus
  `docker_forward_env` for a token.
- **No Obsidian vault mounted**, though the `obsidian` skill is indexed.
- **No Home Assistant path** — see `home-assistant-covers`.
