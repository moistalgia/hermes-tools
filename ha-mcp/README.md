# ha-mcp — Home Assistant MCP server for Hermes

**Covers, lights (incl. Hue), and scenes** behind one interface that never claims
something happened without checking.

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

## Tools (11)

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

### Confirmation strength

- **Covers / lights** — strong. State is re-read and compared to the request.
- **Scenes** — weaker. Scenes have no readable on/off state; HA only records a
  last-triggered timestamp. `confirmed` means "HA accepted and timestamped it."
  For certainty, check individual lights with `list_lights`.

## Verified behavior

Tested over the real MCP stdio protocol (SDK 2.0) plus a mocked HA:

- initialize → `ha 2.0.0`; all **11** tools listed with correct required fields
- missing token → clean error dict, never a traceback
- wrong domain (`light_command` on a `cover.`) → refused with guidance
- on/off-only bulb + `brightness_pct` → `not dimmable`, `permanent: true`
- `rgb_color` on a color-temp-only bulb → refused
- both `color_temp_kelvin` and `rgb_color` → refused
- 6500K request on a 2200–4000K bulb → **clamped to 4000K**, reported in `note`
- brightness 0–255 ↔ percent conversion exact at 0/10/50/100
- cover bitmask: `3 → [open, close]`, `15 → [open, close, set_position, stop]`

Not yet exercised against live HA (no token at build time) — the first
`ha_status` call is the real proof.

## Extending

`_req()` is the single HTTP choke point; `_entities(domain)` normalizes any
domain with area names attached. To add climate or switches, follow the existing
pattern and keep the contract: **read state back, return `confirmed`, name the
cause on failure, mark permanent failures `permanent=True`.**

## Requirements

MCP SDK **2.0+** (`MCPServer`). `FastMCP` was removed in 2.0 — code importing
`mcp.server.fastmcp` fails outright.
