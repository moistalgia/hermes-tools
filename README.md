# hermes-tools

Tools for Hermes/GLADYS, the home agent. One directory per tool.

[DESIGN.md](DESIGN.md) covers how these fit together, the conventions every
server here follows, and what gets built next.

| Server | What it does |
| --- | --- |
| [plex-mcp](plex-mcp/) | Plex playback — search, play, control players, recommend. |
| [state-mcp](state-mcp/) | Shared household memory — tasks, shopping, pantry, meals, appointments, facts. No dependencies. |
| [notify-mcp](notify-mcp/) | Push to your phone, and messages back from it. No dependencies. |
| [hass-mcp](hass-mcp/) | Home Assistant — lights, blinds, thermostats, scenes. No dependencies. |
| [prowlarr-mcp](prowlarr-mcp/) | One search across every indexer, returning magnet links. No dependencies, and drop-in on its own. |
| [qbt-mcp](qbt-mcp/) | Starts a download in qBittorrent and confirms it started. No dependencies. |
| [mcpkit.py](mcpkit.py) | Shared protocol layer. Not a server; imported by the ones above. |
| [scripts/](scripts/) | Host-side jobs. Backups, for now. |
| [tests/](tests/) | `python -m unittest discover -s tests`. No dependencies. |

| Skill | Drives |
| --- | --- |
| [plex-media-playback](skills/plex-media-playback/) | `plex` |
| [media-acquisition](skills/media-acquisition/) | `prowlarr`, `qbt` |
| [home-control](skills/home-control/) | `hass` |
| [household-state](skills/household-state/) | `state` |
| [meal-planning](skills/meal-planning/) | `state` |
| [daily-brief](skills/daily-brief/) | everything |
| [nightly-audit](skills/nightly-audit/) | `state`, `hass`, `notify` |
| [email-triage](skills/email-triage/) | email, `state` |

Every server is also a CLI. Anything the agent can call, you can run from a
shell with identical arguments through identical code — so when something
breaks there is exactly one place it can be breaking:

```bash
python hass-mcp/hass_mcp_server.py set_lights room=office state=on brightness_pct=40
```

## Deploying

Hermes launches MCP servers from its own Python process on the Windows host, so
**every path in the config is a host path.** There is no container path space
and nothing needs mounting.

### Clone

One clone, anywhere `hermes update` does not manage:

```bash
git clone https://github.com/moistalgia/hermes-tools.git E:/hermes-mcp/hermes-tools
```

`state-mcp`, `notify-mcp`, `hass-mcp`, and `prowlarr-mcp` have **no
dependencies** — standard library only. No venv, no install, no packaging.
Hermes runs them with the interpreter it already has, straight from the
checkout.

`plex-mcp` is the exception: it needs `plexapi`, so it keeps its venv and
editable install. See [its README](plex-mcp/README.md).

`prowlarr-mcp` is also the one directory you can copy out on its own — it ships
its own copy of `mcpkit.py` for exactly that, guarded by a test and by a CI step
that runs it from a scratch directory with no repo above it. The rest expect the
layout to stay intact.

### Register

`%USERPROFILE%\.hermes\config.yaml`:

```yaml
mcp_servers:
  state:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/state-mcp/state_mcp_server.py", "serve"]
    env:
      STATE_PERSON: "Nathan"
  notify:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/notify-mcp/notify_mcp_server.py", "serve"]
    env:
      TELEGRAM_TOKEN: "<from @BotFather>"
      TELEGRAM_CHAT_ID: "<see notify-mcp/README.md>"
  hass:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/hass-mcp/hass_mcp_server.py", "serve"]
    env:
      HASS_URL: "http://192.168.1.x:8123"
      HASS_TOKEN: "<long-lived token>"
  prowlarr:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/prowlarr-mcp/prowlarr_mcp_server.py", "serve"]
    env:
      PROWLARR_URL: "http://127.0.0.1:9696"
      PROWLARR_API_KEY: "<Settings → General → API Key>"
  qbt:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/qbt-mcp/qbt_mcp_server.py", "serve"]
    env:
      QBT_URL: "http://127.0.0.1:8080"
      QBT_USER: "admin"
      QBT_PASS: "<Web UI password>"
      QBT_MOVIES_PATH: "P:/Movies"
      QBT_SHOWS_PATH: "P:/Shows"
```

Every credential above is a **literal value**. None of these servers reads a
`.env` file — they run as subprocesses with the environment their parent hands
them and nothing else, so a path to a secrets file is a secret that never
arrives.

The `serve` argument is required and puts the process straight into the
JSON-RPC loop. Omitting it prints usage — to **stderr**, deliberately, so a
mistake in this file produces a readable log rather than a corrupted handshake
with no explanation.

Forward slashes work fine in YAML on Windows and avoid escaping.

### Where the data lives

Three files default to `%USERPROFILE%\.hermes\`, alongside Hermes' own config:

| File | Server | What it is |
| --- | --- | --- |
| `household.db` | `state` | The household's memory |
| `notify.json` | `notify` | Inbound read cursor |
| `house.json` | `hass` | Your Home Assistant room map |

None of them live in the checkout, and that is deliberate: `git pull` would
overwrite `house.json`, and a state store inside a directory you replace is a
state store you lose. Override any of them with `STATE_DB`, `NOTIFY_STATE`, or
`HASS_MAP` if you want them elsewhere.

**Back up `household.db`.** It becomes the thing you rely on before it becomes
the thing you think to protect. [scripts/backup_state.py](scripts/backup_state.py)
does it properly — a verified snapshot through SQLite's backup API rather than a
file copy, which can catch a torn page and look fine until the day you need it:

```bash
python E:/hermes-mcp/hermes-tools/scripts/backup_state.py --keep 30
```

Schedule that daily, then have something run `--check` occasionally: it exits
non-zero when the newest backup has gone stale, which is the only way to tell a
job that stopped running from one that is working.

```bash
schtasks /create /tn "hermes-backup" /tr "python E:/hermes-mcp/hermes-tools/scripts/backup_state.py --keep 30" /sc daily /st 03:30
```

## Tests

```bash
python -m unittest discover -s tests
```

No dependencies, same rule as the servers. They cover the things that go wrong
quietly — date arithmetic, room resolution, partial writes, the stdio handshake
— and [tests/README.md](tests/README.md) explains the fake Home Assistant that
makes read-back verification testable.

## Updating

On the host:

```bash
git -C E:/hermes-mcp/hermes-tools pull
```

The dependency-free servers run from source, so a pull is live — they just need
their MCP connection restarted to reload the file. `plex-mcp`'s editable install
behaves the same way.

`house.json` is the exception and needs no restart at all: `hass-mcp` watches
its modification time and reloads it on the next call, because adding a device
happens often enough that a restart per bulb would be a real tax.

## Import path

Each server imports [mcpkit.py](mcpkit.py) from the repo root using a path
derived from its own `__file__`, never the working directory — so they run
correctly whatever cwd Hermes launches them with. Keep the directory layout
intact and this needs no thought.
