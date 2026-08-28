# ha-mcp — Home Assistant MCP server for Hermes

**Covers, lights (incl. Hue), scenes, native automations, and areas** behind
one interface that never claims something happened without checking.

Install location: `C:\dev\hermes-tools\ha-mcp` (alongside `plex-mcp`).

## Why this exists

Hermes previously drove HA with ad-hoc `curl`, guided by a skill documenting
`ha_call_service`-style tools that **never existed**. When the sandbox container
was rebuilt the token vanished and control silently broke.

This makes correctness structural instead of remembered:

- **`confirmed` on every mutating call.** HA returns 200 for an *accepted*
  request even when hardware never moves. Every command re-reads state and
  reports whether the change actually happened.
- **Capability pre-checks.** Decodes cover `supported_features` and light
  `supported_color_modes`, so asking an on/off bulb to dim gets a clear refusal
  (`permanent: true`) instead of a silent no-op.
- **No guessed entity IDs.** `resolve()` maps "the theater curtains" to real IDs
  across covers/lights/scenes and returns *all* candidates rather than picking.
- **Unambiguous units.** Cover position 0=closed/100=open. Light brightness in
  **percent**. Color temperature in **Kelvin** (mireds were removed from HA in
  2026.3).
- **No polarity assumptions.** Measured per entity, never inferred.
- **Automations are native.** `create_automation` writes a real Home Assistant
  automation — HA's own engine triggers it, this agent is never in the runtime
  path (see [AUTOMATIONS_DESIGN.md](AUTOMATIONS_DESIGN.md) §0). Action steps
  are restricted to light/cover/scene/delay, forever — no locks, alarm,
  garage, scripts, or raw service calls.
- **Areas have no REST API in Home Assistant.** `list_areas`/`create_area`/
  `assign_area` talk over the WebSocket API instead — the only place this
  server uses a second transport.

## Hue

Hue bulbs and Hue scenes arrive as ordinary `light.*` / `scene.*` entities
through Home Assistant, so they work here with **no Hue bridge integration, no
`openhue` CLI, and no extra credentials**. That also sidesteps the fact that the
sandbox container has no `openhue` binary.

## Install (Windows host)

```powershell
cd C:\dev\hermes-tools\ha-mcp
py -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -e .
```

Verify the entry point:

```
C:\dev\hermes-tools\ha-mcp\.venv\Scripts\ha-mcp-serve.exe
```

## Configure

Token: Home Assistant → profile → **Security** → **Long-lived access tokens**.
Store it in `~/.hermes/.env` as `HASS_TOKEN`. Never in `config.yaml`, a
`SKILL.md`, or a repo.

| Env var | Default | Notes |
| --- | --- | --- |
| `HASS_TOKEN` | — | required (`HA_TOKEN` also accepted) |
| `HASS_URL` | `http://127.0.0.1:8123` | `127.0.0.1` is correct — runs host-side |
| `HASS_TIMEOUT` | `15` | seconds |

```powershell
hermes mcp add ha --command "C:/dev/hermes-tools/ha-mcp/.venv/Scripts/ha-mcp-serve.exe"
```

Resulting shape (read, don't hand-edit):

```yaml
mcp_servers:
  ha:
    command: "C:/dev/hermes-tools/ha-mcp/.venv/Scripts/ha-mcp-serve.exe"
    env:
      HASS_URL: "http://127.0.0.1:8123"
      HASS_TOKEN: "${HASS_TOKEN}"
    idle_timeout_seconds: 900
```

Restart Hermes — MCP config loads at startup.

## Verify

```
tool_search(query="home assistant light cover")
tool_call mcp__ha__ha_status        # read-only first
tool_call mcp__ha__list_covers
tool_call mcp__ha__list_lights
```

## Tools (18)

| Tool | Purpose |
| --- | --- |
| `ha_status` | Connection/auth check + entity counts. Call first when broken. |
| `resolve(query, domain?)` | Natural name → candidate entity IDs, scored. |
| `get_state(entity_id)` | Any entity's raw state/attributes. |
| `list_covers(area?)` | Covers: IDs, area, state, position, capabilities. |
| `cover_command(...)` | One cover; returns `confirmed`. |
| `cover_group(...)` | Many covers; per-entity results. |
| `list_lights(area?)` | Lights: IDs, area, brightness %, color capabilities, Kelvin range. |
| `light_command(...)` | One light; brightness %, Kelvin, RGB, transition. |
| `light_group(...)` | Many lights; skips incompatible with a reason. |
| `list_scenes(area?)` | Scenes, including Hue scenes. |
| `activate_scene(...)` | Activate a scene (timestamp-confirmed — see below). |
| `list_areas()` | Every area: id, name, aliases. |
| `create_area(name, aliases?)` | New area; refuses a name/alias collision. |
| `assign_area(entity_id, area)` | Move one entity into an area; read-back confirmed. |
| `list_automations(managed_only?)` | Automations this server made (default), or everything. |
| `get_automation(automation_id)` | One automation's trigger/condition/action config. |
| `create_automation(...)` | Author/update a **native** HA automation. See below. |
| `automation_command(id, action)` | `enable` \| `disable` \| `trigger` \| `delete`. |

### Confirmation strength

- **Covers / lights / automations (enable, disable) / areas** — strong. State
  is re-read and compared to the request.
- **Scenes** — weaker. Scenes have no readable on/off state; HA only records a
  last-triggered timestamp. `confirmed` means "HA accepted and timestamped it."
  For certainty, check individual lights with `list_lights`.
- **`automation_command(..., "trigger")`** — the action ran; whether the
  *result* is what you wanted is only as verifiable as its own action steps
  (a light-turn-on inside it is itself unconfirmed until you check `list_lights`).

### `create_automation` — native, not agent-driven

This writes a real Home Assistant automation. **HA's own engine does the
triggering** — this agent is never in the runtime path, same as a
sunset-triggered porch light configured by hand in the UI. See
[AUTOMATIONS_DESIGN.md](AUTOMATIONS_DESIGN.md) for the full design and why
this doesn't contradict [../DESIGN.md](../DESIGN.md) §7 ("anything
time-triggered stays a native automation").

```python
create_automation(
    alias="Hallway motion -> light",
    triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
    actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on", "brightness_pct": 60}],
)
```

Trigger kinds: `state`, `numeric_state`, `sun` (`event`, `offset_minutes`),
`time` (`at`). Condition kinds (ANDed): the same, plus `and`/`or` nesting.
**Action kinds are permanently allow-listed to `light`, `cover`, `scene`,
`delay`** — the same domains this server exposes for live control, nothing
more. No locks, alarm, garage, scripts, notify, or raw service calls, ever —
an automation is standing infrastructure, and the tiering reasoning is the
same one [../hass-mcp/FUTURE.md](../hass-mcp/FUTURE.md) §1 uses for live
control.

Calling `create_automation` again with the same `alias` **updates** the
existing automation (id defaults to `hermes_<slug(alias)>`) instead of
creating a duplicate. Every automation this creates is labeled
`hermes-managed` in HA's entity registry, which is what `list_automations()`
filters on by default.

## Verified behavior

Tested over the real MCP stdio protocol (SDK 2.0) plus a mocked HA — both
transports (`_req` for REST, `_ws_call` for WebSocket) are swapped for fakes
in [tests/](tests/), run with `.venv\Scripts\python -m unittest discover -s tests`:

- initialize → `ha 2.1.0`; all **18** tools listed with correct required fields
- missing token → clean error dict, never a traceback, **for both transports**
- wrong domain (`light_command` on a `cover.`) → refused with guidance
- on/off-only bulb + `brightness_pct` → `not dimmable`, `permanent: true`
- `rgb_color` on a color-temp-only bulb → refused
- both `color_temp_kelvin` and `rgb_color` → refused
- 6500K request on a 2200–4000K bulb → **clamped to 4000K**, reported in `note`
- brightness 0–255 ↔ percent conversion exact at 0/10/50/100
- cover bitmask: `3 → [open, close]`, `15 → [open, close, set_position, stop]`
- `create_automation` with an unsupported action kind (e.g. `lock`), or a
  capability the target device lacks → refused **before** anything is saved
- `create_automation` called twice with the same alias → updates in place,
  no duplicate entity
- `create_automation(..., enabled=False)` → automation saved off, confirmed
- `automation_command(..., "delete")` → returns the full deleted config
- `list_automations()` → hides hand-written (non-`hermes-managed`) automations
  by default; `managed_only=False` shows everything
- `create_area` → refused on an exact name **or alias** collision
- `assign_area` → ambiguous room name returns candidates, never a guess

Not yet exercised against live HA — see "Verify against a real instance" below.

## Extending

`_req()` is the HTTP choke point; `_ws_call()` is the WebSocket one (areas,
automation labeling — anything with no REST equivalent in Home Assistant).
`_entities(domain)` normalizes any domain with area names attached. To add
climate or switches, follow the existing pattern and keep the contract:
**read state back, return `confirmed`, name the cause on failure, mark
permanent failures `permanent=True`.**

If you add a new `create_automation` action kind, it must reuse the same
capability checks the equivalent live-control tool uses (see `_build_action`)
and go through the same allow-list decision as `../hass-mcp/FUTURE.md` §1 —
never add `lock`/`alarm`/`garage`/`script` as a side effect of something else.

## Verify against a real instance

```powershell
$env:HASS_URL = "http://192.168.1.x:8123"
$env:HASS_TOKEN = "your-long-lived-token"
.\.venv\Scripts\python -m ha_mcp.server
```

Then, from the client (or via `tool_call` in Hermes), in this order:

1. `ha_status` — confirms auth and reachability before anything else.
2. `list_areas` — proves the WebSocket transport works read-only.
3. `create_area(name="ha-mcp test")` — the first WebSocket **write**. Check it
   shows up in Settings → Areas in the HA UI, then delete it there (no
   `delete_area` tool exists here on purpose — deleting a room is not
   something worth a fast path).
4. `list_lights` / `list_covers` — pick one real, low-stakes entity for the
   next steps (a single lamp, not something load-bearing).
5. `assign_area(entity_id=<that light>, area="ha-mcp test")` — confirms the
   WebSocket write-and-read-back path against a real registry.
6. `create_automation(alias="ha-mcp verification", triggers=[{"kind": "time", "at": "23:59:00"}], actions=[{"kind": "light", "entity_id": <that light>, "action": "toggle"}])`
   — creates a real, harmless automation. Check Settings → Automations in the
   HA UI: it should be there, labeled `hermes-managed`, with today's structure.
7. `automation_command(automation_id="hermes_ha_mcp_verification", action="trigger")`
   — runs it immediately rather than waiting for 23:59. Confirm the light
   actually toggled with `list_lights`.
8. `automation_command(automation_id="hermes_ha_mcp_verification", action="delete")`
   — cleans up. Check it is gone from the HA UI too.

Steps 6–8 are the ones worth doing carefully: they are the first time this
server writes something Home Assistant will run **on its own**, unattended.
Use a throwaway light/automation name and a low-stakes entity, the same way
you would not test `cover_command` for the first time on a blind above a
sleeping baby.

## Requirements

MCP SDK **2.0+** (`MCPServer`). `FastMCP` was removed in 2.0 — code importing
`mcp.server.fastmcp` fails outright.
