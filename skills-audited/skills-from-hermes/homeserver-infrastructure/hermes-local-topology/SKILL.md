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

### Consequences you must respect

- **From inside the container**, a host service is `host.docker.internal`, never
  `127.0.0.1`.
- **From an MCP server** (host-side), the same service *is* `127.0.0.1`.
- **Host-side agent tools (`browser_navigate`, `web_extract`, `vision_analyze`)
  refuse localhost/private/internal URLs by design** — an SSRF guard, not a
  bug. Don't try to get it loosened for routine local-dev checks; do
  local-server verification from **inside the container** instead (`curl`
  against `127.0.0.4:<port>`, or the workflow in
  `webapp-predeploy-verification` Gate 3/4).
- **Never** reference `/sandbox/...` paths in MCP code or host-side config.
- Deleting the container kills `terminal` / `read_file` / `execute_code` for
  the session while `web_search`, MCPs, and `skill_manage` keep working.

## The container is Hermes-managed

`terminal.backend: docker`. Hermes spawns **one long-lived container** reused
across tool calls, `/new`, and subagents for the life of the Hermes process.

**Do not `docker run` or `docker compose` a container for this** — that
creates an unrelated container Hermes ignores.

**Restarting the desktop app does NOT reliably recycle the container** — a
stale container can survive a full app restart plus `hermes gateway restart`,
so `docker_forward_env` / `docker_image` changes may not take effect. Check
the container's age against when you changed config:

```bash
stat -c %y /proc/1     # container start time
date                   # now
```

If the container predates your config change, force a new one by stopping it
host-side (`docker stop <id>`) — Hermes provisions a fresh one on the next
terminal call — or fully exit every Hermes process first.

Configure it via `hermes config set` (never hand-edit `config.yaml`):

| Key | Purpose |
| --- | --- |
| `terminal.docker_image` | Base image — bake in deps here |
| `terminal.docker_volumes` | Host↔container mounts (`-v` syntax) |
| `terminal.docker_forward_env` | Forward host env vars (JSON array — see quoting rule below) |
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

Real directories: `/sandbox/in`, `/sandbox/out`, `/sandbox/temp`. There is no
`/sandbox/tmp`. `/root` contains only `.cache`, `.config`, `.hermes` — no
`Windows`, `Users`, or `Program Files`. The host MCP directory
`C:\dev\hermes-tools\` is **not reachable** from the container (correct
isolation).

**Never use Windows-style paths in container commands — POSIX only.**

Rules:
- POSIX paths only inside the container.
- Scratch work in `/tmp`, deliverables in `/sandbox/out`.
- Secrets never in a file on a mount — use host-side secret storage plus
  `terminal.docker_forward_env`.

(Incident history and the cleanup command for a past leak:
`references/incident-log.md`.)

### 9p is slow and will wedge your session

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

The base image ships Python 3.11 + Node 20 and **almost nothing else**.
Absent by default: `pandas`, `numpy`, `openpyxl`, `python-docx`, `python-pptx`,
`PIL`, `requests`, `pyyaml`, `matplotlib` — and the binaries `jq`, `gh`,
`ffmpeg`, `pandoc`, `pygount`.

`pip install` works (network is fine), but it's per-session tax. Skills that
assume `gh` (all `github-*`), `jq` (`gif-search`), or `ffmpeg`
(`ascii-video`, `songsee`) **will fail until installed**. Permanent fix: point
`terminal.docker_image` at a custom image with those baked in.

## Ports (verified from the container)

| Target | Result |
| --- | --- |
| `host.docker.internal:32400` | ✅ Plex, HTTP 401 (auth required — correct) |
| `host.docker.internal:8123` | ✅ Home Assistant, HTTP 200; `/api/` → 401 (needs token) |
| `host.docker.internal:8080` | ✅ some HTTP service, 200 |
| `11434`, `8000`, `1234`, `30000` | ❌ closed — no local LLM server running |

## New MCP tools require a NEW CHAT SESSION

A running session's toolset is fixed at session start. After
`hermes mcp add ...`, the new tools do **not** appear in the current chat, and
`hermes gateway restart` is **not** sufficient — it can even silently drop an
already-working toolset (e.g. all of `terminal`) from the running session.

**Rule:** after any MCP or toolset change, start a new chat session before
testing. If tools are missing mid-session, that is the first thing to
suspect — not a broken server.

Note `tool_search` only reports *deferrable* tools; directly-loaded tools may
be absent from its results while still being callable. To check whether a
tool is truly present, attempt the call — an "is not a deferrable tool" error
means the name is wrong or the tool is not loaded at all.

## A malformed config value kills ALL container tools

`terminal.docker_forward_env` (and friends) are parsed as **strict JSON** at
the start of *every* terminal call. A malformed value silently removes
`terminal`, `read_file`, `write_file`, `patch`, `search_files`, and
`execute_code` from the session's tool list (not a runtime error on the
tool — the tools are just absent) and also blocks MCP tool injection into
that session. Host-side tools (`skill_manage`, `browser_*`, `web_search`,
MCPs, memory) keep working, which makes the failure look narrower than it is.

**Rule:** on Windows, escape inner double quotes for any JSON-valued
`hermes config set` regardless of shell — plain single-quoting is not
enough, in cmd.exe *or* PowerShell (`hermes` goes through a batch shim that
re-parses the argument). Correct form in both shells:

```
hermes config set terminal.docker_forward_env "[\"HASS_TOKEN\",\"TAVILY_API_KEY\"]"
```

**How to tell if the value is mangled** — run `hermes config get` and check
the echo:

| Echo | Meaning |
| --- | --- |
| `["HASS_TOKEN","TAVILY_API_KEY"]` | ✅ valid JSON |
| `[HASS_TOKEN,TAVILY_API_KEY]` | ❌ quotes eaten — broken |
| `'[HASS_TOKEN,TAVILY_API_KEY]'` | ❌ literal string — broken |

Bare unquoted names inside brackets is the tell. Fix it before anything else.
After setting, run one trivial `terminal` call (`echo ok`) to confirm the
value parses before doing real work.

**Diagnostic that settles it in 15 seconds:** spawn a subagent and have it
call `terminal`. A child's toolset is built fresh at spawn time:
- Child **has** `terminal` → config is now correct; your session's toolset is
  just stale. `/new` fixes it. No app restart needed.
- Child **lacks** it too → the config value is still broken.

Full root-cause narrative and history: `references/config-quoting-history.md`.

## A wedged container reports everything as dead

If probes return `http=000` for *every* target, **suspect the container
before concluding the services are down.** A poisoned 9p session returns
empty for all network probes, and may report a Windows `cwd`. Sanity-check
first:

```bash
echo ALIVE    # if this times out, the session is dead, not the network
```

Trust the user's lived experience over a single failed probe.

## Skill platform gating

Skills are filtered by their `platforms:` frontmatter against the **Windows
host**, not the Linux container. So `platforms: [linux, macos]` skills are
**invisible here** and `skill_view` returns
`"not supported on this platform"` — that's the mechanism, not a bug. To use
such a skill, add `windows` to its `platforms` list (only if it genuinely
works host-side).

## Known tool bug

`search_files` silently returns **0 matches** for any path with a
dot-directory ancestor (e.g. `/root/.hermes/skills`) — filesystem-independent,
looks like a clean "no results." Workaround: `grep` with an exact path via
`terminal`. Never trust a zero-result `search_files` under a dot-directory.

## Not currently wired

- **No SSH keys / `.gitconfig`** in the container → cannot push to repos yet.
  Fix via `terminal.docker_volumes` mounting the key read-only plus
  `docker_forward_env` for a token.
- **No Obsidian vault mounted**, though the `obsidian` skill is indexed.
- **No Home Assistant path** — see `home-assistant-covers`.

## Reference

- `references/config-quoting-history.md` — full cmd.exe/PowerShell quoting
  root-cause narrative and the corrected-mistake history for the JSON config
  rule above.
- `references/incident-log.md` — the leaked-credentials incident, the wedged
  HA-declared-down incident, and the MCP-toolset-vanishing incident, in full.
