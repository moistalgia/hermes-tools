# Verified Entity State & Investigation History

Reference material for `home-assistant-covers`. The hot-path `SKILL.md`
already states the actionable conclusions (polarity mapping, entity list,
Hue not wired). This file is the evidence trail and history — read it when
you need to double-check a claim or understand how it was established, not
for routine control.

## Verified entities (2026-08-10, from `/api/states`)

This was the complete set — HA had exactly 3 covers, 0 lights, 0 scenes.

| Entity ID | Friendly name | Area | Reported | ACTUAL physical | Polarity |
| --- | --- | --- | --- | --- | --- |
| `cover.theater_curtain` | Theater Curtain | Theater | `open`, 100 | **CLOSED** | INVERTED ✅ |
| `cover.kitchen_curtain` | Kitchen  Curtain | Kitchen | `open`, 58 | 58% closed | INVERTED ✅ |
| `cover.theater_stairs_curtain` | Theater Stairs Curtain | Theater | `open`, 100 | **CLOSED** | INVERTED ✅ |

`supported_features: 15` = OPEN(1) + CLOSE(2) + SET_POSITION(4) + STOP(8) —
all three support absolute positioning. Note the double space in "Kitchen
 Curtain" (friendly name only; the entity_id is normal).

**Corrections to earlier sessions:** `cover.kitchen_curtain` was never listed
before this and is real. **Bedroom curtains (left/right) DO NOT EXIST** — a
previous session invented them. Never carry forward an entity ID that hasn't
come back from `/api/states`.

## How the polarity inversion was established

Evidence: both theater curtains reported `position: 100` while the user,
standing in the room, confirmed they were physically closed. Standard HA
convention is 0=closed/100=open; this install is the opposite. Only the
theater pair was directly eyeballed — the kitchen curtain's inversion is
assumed from the same device class/integration, not independently confirmed.

HA derives the `state` field from `position != 0`, which is why `state` lies
too:

```json
{"state": "open", "position": 100}   // actually fully CLOSED
```

All three covers currently read `state: "open"` while the theater pair is
shut.

**`open_cover`/`close_cover` direction remains unproven.** Observation only
confirms how *position is reported*, not which way the `open_cover` service
*moves the hardware* — an inverted cover template usually inverts both, but a
partial config can invert reporting only. Until someone tests one cover with
the user watching and records the result here, treat the service direction as
unknown and keep using `set_position`, per the hot-path rule.

## Hue is future-state, not wired

Verified 2026-08-10 against `/api/states` and confirmed by the user: this HA
instance has 42 entities total, 3 covers, ZERO `light.*`, ZERO `scene.*`. The
house runs blinds and media players through HA; the Hue bridge is not
integrated yet — it's on the roadmap, not broken.

An earlier version of this skill claimed Hue bulbs/scenes surface
automatically as `light.*`/`scene.*`. That was never verified and is false
here.

Consequence: 5 of the 18 MCP tools currently have nothing to act on —
`list_lights`, `light_command`, `light_group`, `list_scenes`,
`activate_scene` will return empty. The tools are correct; the entities don't
exist yet. They should start working on their own once the Hue integration is
added to HA — no MCP change needed. Until then, don't promise light or scene
control off this server.

Scenes have no readable on/off state — HA only records a last-triggered
timestamp. For certainty, verify individual lights with `list_lights`
instead.

## Long-term status

As of `ha-mcp` v2.1.0: the server has `confirmed` fields on every mutating
tool, and covers lights, scenes, native automations, and areas. Still not
covered: climate and sensors — those remain `hass-mcp`/`home-control`
territory (a separate, older server with its own `house.json` map), not this
one. The two servers are not merged; don't assume a tool exists here just
because the equivalent exists there.
