# Setup, Fallback & Troubleshooting

Reference material for `home-assistant-covers`. Only needed when the MCP is
missing, broken, or you're setting it up for the first time — not for routine
control.

## Prerequisite: the token

Home Assistant runs at `http://127.0.0.1:8123` (host-side) /
`http://host.docker.internal:8123` (from the container).

### The MCP subprocess needs the token in its OWN env block

MCP stdio servers get a **filtered environment** (only `PATH`, `HOME`, `USER`,
`LANG`, `TERM`, `SHELL`, `TMPDIR`, `XDG_*`). A `HASS_TOKEN` in
`~/.hermes/.env` is **invisible** to `ha-mcp`, and
`terminal.docker_forward_env` is irrelevant here — that forwards into the
*container*, while the MCP runs *host-side*.

Required `config.yaml` shape:

```yaml
mcp_servers:
  ha:
    command: C:/dev/hermes-tools/ha-mcp/.venv/Scripts/ha-mcp-serve.exe
    args: []
    env:
      HASS_TOKEN: <literal token>
    enabled: true
```

`ha-mcp/ha_mcp/server.py` reads (in order): `HASS_URL` → `HA_URL` →
`http://127.0.0.1:8123`; `HASS_TOKEN` → `HA_TOKEN`. The URL default is
already correct host-side, so `HASS_TOKEN` is the only variable you must set.

Use a **literal** token, not `${HASS_TOKEN}` — `${VAR}` expansion is
documented for `command`/`args`/`url`/`headers` but not for `env`, and
upstream issue #11239 is an open *request* for env-backed secret refs in MCP
config.

| Error | Meaning |
| --- | --- |
| `HASS_TOKEN is not set` | no `env:` block reaching the subprocess |
| `401 unauthorized - HASS_TOKEN is invalid or expired` | env block works, token wrong/empty/placeholder |
| `ok: true` | good |

`env` is read at **subprocess spawn**, so after editing: `/reload-mcp` then
`/new`. A gateway restart is not sufficient.

Needs a long-lived access token (HA → profile → Security → Long-lived access
tokens). The variable name is **`HASS_TOKEN`**.

To expose it to the container for curl work:

```
hermes config set terminal.docker_forward_env '["HASS_TOKEN","TAVILY_API_KEY"]'
```

**Never** put the token in a SKILL.md, `config.yaml`, or a command you echo
back. If `$HASS_TOKEN` is unset in the container, say so and stop — do not
ask the user to paste it inline.

## New MCP tools need a fresh chat session

Per `hermes-local-topology`, a session's toolset is fixed at start — after
`ha-mcp` is rebuilt/redeployed with new tools and Hermes reloads the server,
they will not resolve in an *already-running* session. Start a new one before
testing new tools; if they seem to not exist, check this first before
assuming the server is broken.

## Fallback: REST via curl

If the MCP isn't installed yet, drive the REST API directly. It works, but
you must verify state yourself — exactly the discipline the MCP makes
structural.

```bash
export HA="http://host.docker.internal:8123"
AUTH="Authorization: Bearer $HASS_TOKEN"
```

Note the host: `host.docker.internal` from the container, but `127.0.0.1`
from the host-side MCP server.

### Discover entities

```bash
timeout 15 curl -s -H "$AUTH" "$HA/api/states" \
  | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    if s['entity_id'].startswith('cover.'):
        a=s['attributes']
        print(s['entity_id'], '|', a.get('friendly_name'), '| state=', s['state'],
              '| pos=', a.get('current_position'), '| class=', a.get('device_class'))
"
```

### Commands

```bash
# open / close / stop
timeout 15 curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"entity_id":"cover.theater_curtain"}' \
  "$HA/api/services/cover/open_cover"

# set a percentage (0-100)
timeout 15 curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"entity_id":"cover.theater_curtain","position":50}' \
  "$HA/api/services/cover/set_cover_position"
```

Service endpoints: `cover/open_cover`, `cover/close_cover`, `cover/stop_cover`,
`cover/set_cover_position`.

### Always verify

A `200` means the request was accepted, not that anything moved.

```bash
timeout 15 curl -s -H "$AUTH" "$HA/api/states/cover.theater_curtain" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('state:', d['state'], 'position:', d['attributes'].get('current_position'))
"
```

Covers report `opening`/`closing` while moving — wait ~2s and re-read for a
settled state.

## Areas have no REST fallback

Unlike everything else here, area reads/writes go over Home Assistant's
WebSocket API — there is no REST equivalent. If the MCP is down, area work
simply waits; there's no manual workaround worth documenting.

## Pitfalls

| Issue | Reality |
| --- | --- |
| `127.0.0.1:8123` from container | Wrong host — use `host.docker.internal` |
| `401 Unauthorized` | Token missing/expired, or not forwarded into the container |
| HTTP 200 means it moved | No. Read state back. |
| `ha_call_service` tool | Does not exist. There are no bare `ha_*` tools — an earlier version of this skill described `ha_call_service(...)`/`ha_list_entities(...)`; those were never real. Use the MCP tools or REST. |
| Empty/`000` response from HA | Suspect a wedged container before concluding HA is down |
| Instant state after a command | Covers report `opening`/`closing`; wait and re-read |

## Provenance

Everything in the hot-path `SKILL.md` was verified 2026-08-10 against a live
instance: HA 2026.7.1, `ha_status` → `ok: true`, all read-only tools
(`ha_status`, `list_covers`, `list_lights`, `list_scenes`, `resolve`)
confirmed working end-to-end. Mutating tools (`cover_command`, `cover_group`)
were untested against hardware as of that date. `resolve` scored "kitchen
curtain" at 0.968 for `cover.kitchen_curtain` vs 0.733/0.595 for the theater
pair, and correctly flagged multi-match cases for confirmation rather than
guessing.

A prior recon session wrongly declared HA down entirely — those probes ran
through a wedged container returning `http=000` for everything test-wide. HA
was fine; see the topology skill's "wedged container" note.
