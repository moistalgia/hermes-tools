---
name: home-assistant-covers
description: "HA covers, lights (incl. Hue), scenes, automations, and areas via MCP."
version: 5.1.0
author: gladys + moisty
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [smart-home, home-assistant, covers, blinds, curtains, lights, hue, scenes, automations, areas, iot]
    related_skills: [mcp-authoring, hermes-local-topology]
---

# Home Assistant — Covers, Lights, Scenes, Automations & Areas

**Quick reference:** `resolve("name")` → get the entity ID → `cover_command`/
`light_command`/`activate_scene` → check `confirmed`. For covers on this
install, position is inverted: **100 = closed, 0 = open** — always use
`set_position`, never `open_cover`/`close_cover`.

The name is legacy (covers came first); this is the one skill for everything
the `ha` MCP server does.

## When to Use

Any request to control a blind, curtain, shade, garage door, light, or scene;
to set up/change/remove a recurring automation; or to move a device into a
room/area. Also load when HA behavior seems backwards, or the user asks about
Hue.

## Tools

| Tool | Purpose |
| --- | --- |
| `mcp__ha__ha_status` | Connection/auth check + entity counts — call first when broken |
| `mcp__ha__resolve` | "theater curtains" → candidate entity IDs (any domain) |
| `mcp__ha__get_state` | Any entity's raw state |
| `mcp__ha__list_covers` | Covers: IDs, area, state, position, capabilities |
| `mcp__ha__cover_command` | One cover; returns `confirmed` |
| `mcp__ha__cover_group` | Many covers; per-entity results |
| `mcp__ha__list_lights` | Lights: brightness %, color modes, Kelvin range |
| `mcp__ha__light_command` | One light; brightness %, Kelvin, RGB, transition |
| `mcp__ha__light_group` | Many lights; skips incompatible with a reason |
| `mcp__ha__list_scenes` | Scenes (incl. Hue scenes) |
| `mcp__ha__activate_scene` | Activate a scene |
| `mcp__ha__list_areas` | Every area: id, name, aliases |
| `mcp__ha__create_area` | New area; refuses a name/alias collision |
| `mcp__ha__assign_area` | Move one entity into an area |
| `mcp__ha__list_automations` | Automations this server made (default), or everything |
| `mcp__ha__get_automation` | One automation's trigger/condition/action config |
| `mcp__ha__create_automation` | Author or update a **native** HA automation |
| `mcp__ha__automation_command` | `enable` \| `disable` \| `trigger` \| `delete` one automation |

**This HA instance currently has 0 lights and 0 scenes** (Hue isn't wired in
yet) — `list_lights`/`light_command`/`list_scenes`/`activate_scene` will
return empty until it is. See `references/verified-state.md` if a light/scene
request comes up.

## Workflow

1. `resolve` or a `list_*` to get real entity IDs — never guess them. A
   multi-match result means confirm with the user before acting.
2. Send the command.
3. Check `confirmed` in the response. Never report a change unless it's true.
4. For covers, re-read `position` (not `state`) if the change matters —
   covers report `opening`/`closing` while moving.

**Units:** cover position 0=closed/100=open by HA convention (see inversion
below); light brightness in **percent**; color temperature in **Kelvin**
(mireds removed from HA in 2026.3).

## Cover polarity — INVERTED on this install

| Position | Physical reality |
| --- | --- |
| **100** | **fully CLOSED** |
| **0** | **fully OPEN** |
| 58 | 58% closed |

- Never report open/closed from the `state` field — it's derived from
  `position != 0`, so `state: "open"` at `position: 100` means **closed**.
  Read `position` and apply the inversion; translate to physical terms for
  the user, never parrot the raw number.
- **Prefer `set_position` over `open_cover`/`close_cover`.** The position
  mapping above is confirmed; which physical direction `open_cover`/
  `close_cover` actually drive is not. To close: `set_position` **100**. To
  open: `set_position` **0**.
- All 3 covers on this instance (`cover.theater_curtain`,
  `cover.kitchen_curtain`, `cover.theater_stairs_curtain`) share this
  inversion. Full entity table and how this was established:
  `references/verified-state.md`.

## Automations — `create_automation` writes a NATIVE HA automation

HA's own engine runs it from then on; this agent is not in the runtime path
once it's saved. That's why testing uses `automation_command(id, "trigger")`
to fire it immediately rather than waiting for the real trigger.

```python
mcp__ha__create_automation(
    alias="Hallway motion -> light",
    triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
    actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on", "brightness_pct": 60}],
)
```

- **Trigger kinds:** `state` (`entity_id`, `to`, `from_`, `for_seconds`),
  `numeric_state` (`entity_id`, `above`, `below`, `for_seconds`), `sun`
  (`event`: `"sunrise"`/`"sunset"`, `offset_minutes`), `time` (`at`:
  `"HH:MM:SS"`). **Conditions** (ANDed): same four, plus `and`/`or` nesting.
- **Action kinds are limited to `light`, `cover`, `scene`, `delay`.** For a
  lock, alarm, garage, script, or raw service call: say plainly it's not
  available, don't look for a workaround.
- Every `entity_id` must come from `resolve()`/`list_*` first, same as live
  control. For a cover action, apply the **same inverted `position` mapping**
  as above (`position: 100` to close, `position: 0` to open) — an automation
  built with the un-inverted value runs backwards every day, silently.
- Calling `create_automation` again with the same `alias` **updates** the
  existing automation (id defaults to `hermes_<slug(alias)>`) instead of
  duplicating it — check `list_automations()` first regardless.
- `list_automations()` defaults to `hermes-managed` automations only; pass
  `managed_only=False` to see hand-written ones too, but treat those as
  read-only.
- `automation_command(id, "delete")` has no undo. The response includes the
  full deleted config — hold onto it until the user confirms the delete was
  wanted.

## Areas

`assign_area(entity_id, area)` sets the entity's own area, overriding
whatever area its device has. `area` matches fuzzily like `resolve()`; an
ambiguous/unknown name comes back as candidates, never a guess —
`list_areas()` first if unsure. `create_area` refuses a name/alias collision.
Area reads/writes have no REST fallback (WebSocket-only) — if the MCP is
down, area work waits.

## Safety

Confirm before moving covers in occupied rooms, and before anything
security-relevant (garage doors). This extends to automations: confirm
before creating one that will run unattended — getting it wrong is a
standing problem, not a one-time one.

## If something's broken

MCP down, token issues, `401`s, REST/curl fallback, or the tool list not
matching this file: see `references/setup-and-troubleshooting.md`. Full
verified-entity table, Hue status, and how the polarity finding was
established: `references/verified-state.md`.
