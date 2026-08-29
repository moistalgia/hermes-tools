---
name: home-assistant-covers
description: "HA covers, lights (incl. Hue), scenes, automations, and areas via MCP."
version: 5.0.0
author: gladys + moisty
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [smart-home, home-assistant, covers, blinds, curtains, lights, hue, scenes, automations, areas, iot]
    related_skills: [mcp-authoring, hermes-local-topology]
---

# Home Assistant — Covers, Lights, Scenes, Automations & Areas

## When to Use

Any request to control a blind, curtain, shade, garage door, **light, or
scene** in the house; any request to **set up, change, or remove a recurring
automation** ("turn the hall light on with motion", "open the blinds at
sunrise"); or any request to **move a device into a room/area**. Also load
when diagnosing HA behavior that seems backwards, or when the user asks about
Hue.

**The name is legacy.** This skill covered covers first and kept its name;
it is the one skill for everything the `ha` MCP server does, including the
automation and area tools added in `ha-mcp` v2.1.0.

## Status

Home Assistant **is live and the MCP is fully working**. Verified 2026-08-10 via
`mcp__ha__ha_status` → `ok: true`:

- HA **2026.7.1**, location `Home`, state `RUNNING`
- URL `http://127.0.0.1:8123` (host-side, from the MCP)
- **3 covers, 0 lights, 0 scenes**

All read-only tools confirmed working end-to-end: `ha_status`, `list_covers`,
`list_lights`, `list_scenes`, `resolve`. Mutating tools (`cover_command`,
`cover_group`) are **untested against hardware** as of this date.

`resolve` works well — "kitchen curtain" scored 0.968 for `cover.kitchen_curtain`
vs 0.733/0.595 for the theater pair, and it correctly returned
`note: "multiple matches; confirm with the user before acting"`. Respect that
note; don't act on a multi-match without confirming.

### Prerequisite: the token

Home Assistant **is live** at `http://127.0.0.1:8123` (host-side) /
`http://host.docker.internal:8123` (from the container). Verified 2026-08-10:
`manifest.json` confirms it and `/api/` returns a clean `401`.

### Preferred path: the `ha` MCP server

A purpose-built HA MCP server lives at `C:\dev\hermes-tools\ha-mcp` (source also
staged at `/sandbox/out/ha-mcp`), tested end-to-end over the real MCP protocol.
**18 tools** covering covers, lights, scenes, native automations, and areas
(added in v2.1.0 — the last 7 rows below are new and, as of this skill
version, **not yet exercised against this live instance**; everything above
them is the same code path already verified 2026-08-10):

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

**Workflow:** `resolve` or a `list_*` first → command → check `confirmed`.
Never report a change unless `confirmed` is true.

**New MCP tools need a fresh chat session.** Per `hermes-local-topology`, a
session's toolset is fixed at start — after `ha-mcp` is rebuilt/redeployed
with the new tools and Hermes reloads the server, `mcp__ha__create_automation`
etc. will not resolve in an *already-running* session. Start a new one before
testing any of the 7 new tools; if they seem to not exist, that's the first
thing to check, not a broken server.

**Units:** cover position 0=closed/100=open; light brightness in **percent**;
color temperature in **Kelvin** (mireds removed from HA in 2026.3).

**Hue is future-state, not yet wired.** Verified 2026-08-10 against
`/api/states` and confirmed by the user: this HA instance has **42 entities,
3 covers, ZERO `light.*`, ZERO `scene.*`**. The house runs blinds and media
players through HA; the Hue bridge is **not** integrated yet — it's on the
roadmap, not broken.

An earlier version of this skill claimed Hue bulbs/scenes surface automatically
as `light.*`/`scene.*`. That was never verified and is **false here**.

Consequence: **5 of the 11 MCP tools currently have nothing to act on** —
`list_lights`, `light_command`, `light_group`, `list_scenes`, `activate_scene`
will return empty. The tools are correct; the entities don't exist yet. They
should start working on their own once the Hue integration is added to HA — no
MCP change needed. Until then, don't promise light or scene control off this
server.

**Scene confirmation is weaker.** Scenes have no readable on/off state; HA only
records a last-triggered timestamp. For certainty, verify individual lights with
`list_lights`.

## Automations — `create_automation` writes a NATIVE HA automation

`mcp__ha__create_automation` does not run anything itself. It writes a real
Home Assistant automation and **HA's own engine does the triggering** from
then on — same as one built by hand in the HA UI. This agent is never in the
runtime path once it's saved. That's the point: "motion in the hallway turns
on the light" and "open the blinds at sunrise" are exactly the kind of thing
that belongs in HA natively, not in a polling loop here.

```python
mcp__ha__create_automation(
    alias="Hallway motion -> light",
    triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
    actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on", "brightness_pct": 60}],
)
```

**Trigger kinds:** `state` (`entity_id`, `to`, `from_`, `for_seconds`),
`numeric_state` (`entity_id`, `above`, `below`, `for_seconds`), `sun`
(`event`: `"sunrise"`/`"sunset"`, `offset_minutes`), `time` (`at`:
`"HH:MM:SS"`). **Condition kinds** (ANDed): the same four, plus `and`/`or`
nesting. **Action kinds are permanently limited to `light`, `cover`, `scene`,
`delay`** — the same domains this server exposes for live control and
nothing more. If a request needs a lock, the alarm, the garage, a script, or
a raw service call, **say plainly this is not available and do not look for
a workaround** — same rule this skill already states for those domains
elsewhere. Every `entity_id` must come from `resolve()`/`list_*` first,
exactly as for live control.

**⚠️ Cover automations inherit this house's inverted polarity, AND the same
open_cover/close_cover caveat applies.** The three covers on this instance
report position **backwards** (see the Polarity section below —
`position: 100` is physically CLOSED here), and that inversion is in what HA
itself reports, so it is *also* what `create_automation`'s
`{"kind": "cover", "action": "position", "position": N}` sets physically. An
automation meant to open the theater curtain at sunrise needs
`position: 0`... except **use the same operating rule as live control**:
`open_cover`/`close_cover`'s physical direction is still unproven on these
covers, while `set_position`'s mapping is confirmed by direct observation.
So for these three covers, build the automation action as
`{"kind": "cover", "action": "position", "position": <inverted value>}` —
**`position: 100` to close, `position: 0` to open** — and avoid
`{"action": "open"}` / `{"action": "close"}`, exactly mirroring "Prefer
`set_position` over `open_cover`/`close_cover`" above. Get the number backwards
and the automation runs the wrong way **every single day**, silently — it's a
syntactically valid automation doing the opposite of what was asked, and
nothing here would flag it as an error.

**Calling `create_automation` again with the same `alias` updates the
existing automation** (id defaults to `hermes_<slug(alias)>`) instead of
creating a duplicate — check `list_automations()` first regardless, so
"set up a motion light in the hallway" said twice doesn't read as two
separate requests.

**`list_automations()` defaults to `hermes-managed` automations only** — the
ones this server created and labeled. Nothing hand-written in the HA UI shows
up unless `managed_only=False` is passed, and even then, treat someone else's
hand-built automation as read-only — this skill has no business editing logic
it doesn't understand.

**`automation_command(id, "delete")` has no undo in Home Assistant.** The
tool response includes the full deleted config specifically so it can be
recreated with `create_automation` if the deletion turns out to be a mistake
— hold onto that response until the user confirms the delete was wanted, the
same discipline as any other destructive action.

**Testing a new automation:** use `automation_command(id, "trigger")` to run
its actions immediately rather than waiting for the real trigger (sunrise,
motion, whatever) — this is the fastest way to prove an automation actually
does what was intended before trusting it to run unattended.

## Areas — `assign_area` has no REST fallback

Unlike everything else in this section, area reads/writes go over Home
Assistant's WebSocket API — there is no REST equivalent, so **the `curl`
fallback in this skill does not cover areas.** If the MCP is down, area work
simply waits; there's no manual workaround worth documenting.

`assign_area(entity_id, area)` sets the entity's own area, which overrides
whatever area its *device* has — correct for "the theater lamp is in the
theater" regardless of what physical device it's paired with. `area` is
matched fuzzily the same way `resolve()` matches devices; an ambiguous or
unknown name comes back as candidates, never a guess — call `list_areas()`
first if unsure what exists. `create_area` refuses a name/alias collision
rather than making a confusingly similar second room.

**There are no `ha_*` bare tools.** Earlier versions of this skill instructed
calling `ha_call_service(...)` / `ha_list_entities(...)`; those never existed.
A prior recon also wrongly declared HA down — those probes ran through a wedged
container returning `http=000` for everything. HA was fine.

### Fallback: REST via curl

If the MCP isn't installed yet, drive the REST API directly (below). It works,
but you must remember to verify state yourself — exactly the discipline the MCP
makes structural.

### ⚠️ The MCP subprocess needs the token in its OWN env block

Verified 2026-08-10 — this cost a full debugging session. MCP stdio servers get a
**filtered environment** (only `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `SHELL`,
`TMPDIR`, `XDG_*`). A `HASS_TOKEN` in `~/.hermes/.env` is **invisible** to
`ha-mcp`, and `terminal.docker_forward_env` is irrelevant here — that forwards
into the *container*, while the MCP runs *host-side*.

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
`http://127.0.0.1:8123`; `HASS_TOKEN` → `HA_TOKEN`. **The URL default is already
correct host-side, so `HASS_TOKEN` is the only variable you must set.**

Use a **literal** token, not `${HASS_TOKEN}` — `${VAR}` expansion is documented
for `command`/`args`/`url`/`headers` but not for `env`, and upstream issue #11239
is an open *request* for env-backed secret refs in MCP config.

**Read the error text — the server distinguishes the three failures:**

| Error | Meaning |
| --- | --- |
| `HASS_TOKEN is not set` | no `env:` block reaching the subprocess |
| `401 unauthorized - HASS_TOKEN is invalid or expired` | env block works, token wrong/empty/placeholder |
| `ok: true` | good |

`env` is read at **subprocess spawn**, so after editing: `/reload-mcp` then
`/new`. A gateway restart is not sufficient.

Needs a long-lived access token (HA → profile → Security → Long-lived access
tokens). The variable name is **`HASS_TOKEN`**.

It is already present host-side. To expose it to the container for curl work:

```
hermes config set terminal.docker_forward_env '["HASS_TOKEN","TAVILY_API_KEY"]'
```

**Never** put the token in a SKILL.md, `config.yaml`, or a command you echo
back. If `$HASS_TOKEN` is unset in the container, say so and stop — do not ask
the user to paste it inline.

```bash
export HA="http://host.docker.internal:8123"
AUTH="Authorization: Bearer $HASS_TOKEN"
```

Note the host: **`host.docker.internal` from the container**, but `127.0.0.1`
from the host-side MCP server. See `hermes-local-topology`.

## Step 1 — discover entities (never guess IDs)

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

Use returned IDs **verbatim**. Note `supported_features` — not every cover
supports positioning.

## Step 2 — commands

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

## Step 3 — ALWAYS verify

A `200` means the request was accepted, **not** that anything moved.

```bash
timeout 15 curl -s -H "$AUTH" "$HA/api/states/cover.theater_curtain" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('state:', d['state'], 'position:', d['attributes'].get('current_position'))
"
```

Covers report `opening`/`closing` while moving — wait ~2s and re-read for a
settled state. Never claim a blind moved without a state read backing it.

## Polarity — INVERTED. Confirmed by direct observation 2026-08-10

**These covers report position backwards from the HA convention:**

| Position | Physical reality |
| --- | --- |
| **100** | **fully CLOSED** |
| **0** | **fully OPEN** |
| 58 | 58% closed — just past halfway, on the closed side |

Evidence: both theater curtains report `position: 100` while the user, standing
in the room, confirms they are **physically closed**. Standard HA is
0=closed/100=open. This install is the opposite.

### ⚠️ The `state` field lies too — this is the dangerous part

HA derives `state` from `position != 0`, so it reports:

```json
{"state": "open", "position": 100}   // ← actually fully CLOSED
```

Every one of the three covers currently reads `state: "open"` while the theater
pair is shut. **`state: open` on these entities means CLOSED.**

**Operating rules:**

- **Never** report open/closed from the `state` field. Read `position` and apply
  the inversion.
- Translate for the user in physical terms: `position: 100` → "closed". Never
  parrot the raw number as if it meant openness.
- **Prefer `set_position` over `open_cover`/`close_cover`.** Position mapping is
  confirmed; the *service* polarity is **not** (see below).

### Still unproven: `open_cover` / `close_cover` direction

Observation confirms how position is **reported**. It does not prove which way
the `open_cover` service **moves** the hardware. An inverted cover template
usually inverts both, but a partial config can invert reporting only.

Until tested, drive covers with `set_position` (0 = open, 100 = closed) where the
mapping is known. If you must use `open_cover`/`close_cover`, test one cover with
the user watching and record the result here.

To close: `set_position` **100**. To open: `set_position` **0**.

## Verified entities

Read from `/api/states` with a valid token on **2026-08-10**. These are the
complete set — HA has exactly 3 covers.

| Entity ID | Friendly name | Area | Reported | ACTUAL physical | Polarity |
| --- | --- | --- | --- | --- | --- |
| `cover.theater_curtain` | Theater Curtain | Theater | `open`, 100 | **CLOSED** | INVERTED ✅ |
| `cover.kitchen_curtain` | Kitchen  Curtain | Kitchen | `open`, 58 | 58% closed | INVERTED ✅ |
| `cover.theater_stairs_curtain` | Theater Stairs Curtain | Theater | `open`, 100 | **CLOSED** | INVERTED ✅ |

Inversion confirmed 2026-08-10 by user observation of the physically-closed
theater pair reporting `position: 100` / `state: open`. Assume all three share it
(same device class, same integration) but note only the theater pair was directly
eyeballed.

`supported_features: 15` = OPEN(1) + CLOSE(2) + SET_POSITION(4) + STOP(8) — all
three support absolute positioning. Note the double space in "Kitchen  Curtain"
(friendly name only; the entity_id is normal).

**Corrections to earlier sessions:** `cover.kitchen_curtain` was never listed
before and is real. **Bedroom curtains (left/right) DO NOT EXIST** — a previous
session invented them. Never carry forward an entity ID that hasn't come back
from `/api/states`.

## Pitfalls

| Issue | Reality |
| --- | --- |
| `127.0.0.1:8123` from container | Wrong host — use `host.docker.internal` |
| `401 Unauthorized` | Token missing/expired, or not forwarded into the container |
| HTTP 200 means it moved | No. Read state back. |
| `ha_call_service` tool | Does not exist. Use REST. |
| **`state: open` means open** | **NO — inverted here. `open`/100 = physically CLOSED.** |
| **`position: 100` means open** | **NO — 100 = closed, 0 = open on this install.** |
| Empty/`000` response from HA | Suspect a wedged container before concluding HA is down |
| Instant state after a command | Covers report `opening`/`closing`; wait and re-read |

## Safety

Confirm before moving covers in occupied rooms, and before anything
security-relevant (garage doors). A heads-up beats a surprise. **This extends
to automations**: confirm before creating one that will run unattended,
same as any physical action — the difference is it keeps happening after
this conversation ends, so getting it wrong is a standing problem, not a
one-time one.

## Long-term

Done, as of `ha-mcp` v2.1.0: this MCP server now has `confirmed` fields on
every mutating tool, and covers lights, scenes, native automations, and
areas. Still not covered: climate and sensors — those remain
`hass-mcp`/`home-control` territory (a separate, older server with its own
`house.json` map), not this one. The two servers are not merged; don't
assume a tool exists here just because the equivalent exists there.
