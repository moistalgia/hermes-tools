# Design: automations + areas for `ha-mcp`

Status: proposed, not implemented. This is the spec an implementing agent should
follow. Sourced against Home Assistant's REST API, WebSocket API, and current
(2024.10+) automation YAML schema — see citations inline. Where the API is
undocumented-but-stable (the automation config endpoint), that is called out
explicitly rather than papered over.

---

## 0. Why this belongs here, and why it doesn't violate §7

[`../DESIGN.md`](../DESIGN.md) §7 draws a hard line: anything time-triggered or
sensor-triggered stays a **native Home Assistant automation**; the agent is the
interface and the exception handler, not the control loop. That line is not
being moved. This proposal adds tools that **author and maintain native HA
automations** — HA's own automation engine still does the triggering,
locally, in milliseconds, with the agent nowhere in the runtime path. What
changes is *who writes the YAML*: today it's a human in the HA UI; after this,
it can be the agent, through the same `resolve → act → verify` discipline
every other tool in this server already follows.

"Motion detected → turn on lights" and "sunrise → lights + blinds to X%" are
exactly the automations table in §7 says belong in HA. This is the tool that
puts them there.

Area assignment is unrelated to triggering but shares a machine — the
registry that says which room an entity lives in is adjacent to the map this
server already builds in `_areas()` — so it travels with this proposal rather
than getting its own doc.

---

## 1. Two new capability groups, one new transport

| Group | New tools | Talks to |
| --- | --- | --- |
| **Automations** | `list_automations`, `get_automation`, `create_automation`, `update_automation`, `automation_command`, `delete_automation` | REST (`/api/config/automation/config/*`, `/api/services/automation/*`) |
| **Areas** | `list_areas`, `create_area`, `assign_area` | **WebSocket** (`/api/websocket`) |

Area, device, and entity registry writes have **no REST equivalent** — HA
only exposes them over the WebSocket API.
[Confirmed against the frontend's own source](https://developers.home-assistant.io/docs/api/websocket/):
`config/area_registry/create|update|delete|list|reorder` and
`config/entity_registry/update` (which accepts `area_id`) are WebSocket-only
commands. This means `ha-mcp` needs a second transport, not just new REST
calls. Section 4 below specs it as a single new choke point, `_ws_call()`,
that mirrors the existing `_req()` exactly — same failure shape, same
"one place every call goes through" property DESIGN.md §2 requires of `_req`.

Add `websockets` (or `httpx-ws`; `websockets` is smaller and this project
already avoids frameworks) to `pyproject.toml`.

---

## 2. Automations

### 2.1 The endpoint, and its caveat

`POST /api/config/automation/config/<id>` creates or overwrites an automation;
`GET` on the same path reads it back; `DELETE` removes it. This is the same
endpoint HA's own automation editor uses. It is **not in the official REST API
docs** — the documented surface stops at `/api/states`, `/api/services`,
`/api/events`, `/api/template`, `/api/config`, `/api/config/core/check_config`
— but it has been stable for years and is what every third-party integration
that writes automations actually uses.
([community thread confirming the shape](https://community.home-assistant.io/t/rest-api-docs-for-automations/119997))

**Caveat that matters for "maintain":** this endpoint only manages automations
stored in HA's own storage collection (`automations.yaml` by default, edited
through the UI). It has no effect on automations defined inside `configuration
packages` or split across custom YAML includes. `list_automations` (2.4)
should report which automations are and are not editable through this path,
so the agent never tells someone it updated something it silently didn't.

A `POST` validates the payload server-side and returns 400 with the
validation error, without persisting anything, if the config is malformed —
so the "confirm-before-commit" step for automations is built into the write
itself. What it does *not* verify is that entity_ids inside the automation
exist or that referenced services are callable — HA will happily save an
automation that turns on `light.does_not_exist`. That validation is this
server's job (2.3).

### 2.2 Schema this server emits

HA's automation schema was renamed in 2024.10: top-level `trigger`/
`condition`/`action` became `triggers`/`conditions`/`actions`; individual
trigger items now use the key `trigger:` (not the old `platform:`); action
steps now use `action:` (not the old `service:`). Old keys still parse for
backward compatibility, but everything this server *emits* should use the
current schema, since anything written today can reasonably expect years of
life.
([home-assistant.io/docs/automation/yaml](https://www.home-assistant.io/docs/automation/yaml/),
[.../automation/trigger](https://www.home-assistant.io/docs/automation/trigger/))

```yaml
id: "hermes_hallway_motion_light"          # ours: see 2.5 for id policy
alias: "Hallway motion → light"
description: "Hermes-managed. Edit via ha-mcp, not the UI, or the two will drift."
triggers:
  - trigger: state
    entity_id: binary_sensor.hallway_motion
    to: "on"
conditions: []
actions:
  - action: light.turn_on
    target:
      entity_id: light.hallway
    data:
      brightness_pct: 60
mode: single
```

### 2.3 Tool surface — typed builder, not raw YAML passthrough

DESIGN.md §2 is explicit: *"Verbs, not a dispatcher... a generic service call
re-imports every guess the map just eliminated."* A tool that accepts raw
automation YAML is exactly that dispatcher, aimed at something with more
blast radius than a single light. So `create_automation` / `update_automation`
take **structured trigger/condition/action objects**, not YAML text and not a
free-form service call. Each object type maps 1:1 onto a domain this server
already curates, and gets the *same* capability checks the live-control tools
already enforce — `create_automation` should refuse to schedule a
non-dimmable porch light to dim at sunset for the identical reason
`light_command` refuses it live. Concretely: the action builder should call
the same internal helpers `light_command`/`cover_command` use for capability
lookups (`_entities()`, `dimmable`, `supports_color_temp`, cover `supports`)
before it will emit a step, not just before it will run one.

```python
Trigger = dict[str, Any]
# {"kind": "state", "entity_id": str, "to": str | None, "from_": str | None,
#  "for_seconds": int | None}
# {"kind": "sun", "event": "sunrise" | "sunset", "offset_minutes": int}
# {"kind": "time", "at": "HH:MM:SS"}
# {"kind": "numeric_state", "entity_id": str, "above": float | None,
#  "below": float | None, "for_seconds": int | None}

Condition = dict[str, Any]
# same "kind" vocabulary as Trigger (state / numeric_state / sun / time),
# plus {"kind": "and"/"or", "conditions": list[Condition]}

Action = dict[str, Any]
# {"kind": "light", "entity_id": str, "action": "on"/"off"/"toggle",
#  "brightness_pct": int | None, "color_temp_kelvin": int | None,
#  "rgb_color": list[int] | None, "transition": float | None}
# {"kind": "cover", "entity_id": str, "action": "open"/"close"/"position",
#  "position": int | None}
# {"kind": "scene", "entity_id": str}
# {"kind": "delay", "seconds": float}
```

```python
@mcp.tool()
def create_automation(
    alias: str,
    triggers: list[Trigger],
    actions: list[Action],
    conditions: list[Condition] | None = None,
    mode: str = "single",
    enabled: bool = True,
    automation_id: str | None = None,   # omit to derive from alias; see 2.5
) -> dict[str, Any]:
    """Author a NATIVE Home Assistant automation - HA runs it, not this agent.

    Every entity_id in every trigger/condition/action must come from resolve()
    or a list_* tool first, exactly as for live control. Action steps are
    checked against the same capability rules as light_command/cover_command
    (a non-dimmable light with brightness_pct set is refused here too, before
    anything is saved). Restricted to light/cover/scene/delay actions - see
    §3 for why locks, alarm, garage, and arbitrary services are refused.
    """
```

`update_automation(automation_id, ...)` takes the same shape and does a
read-modify-write: GET current config first (so a partial-field update is
possible and so the diff can be reported), then POST the merged result.

`automation_command(automation_id, action)` with `action` in
`enable | disable | trigger | delete`, mapping to
`POST /api/services/automation/turn_on|turn_off|trigger` and
`DELETE /api/config/automation/config/<id>`. `trigger` exists specifically so
"sunrise → open the blinds" can be tested at 2pm instead of waited for.

### 2.4 Read-back verification (the §3 pattern, applied here)

A `POST` returning 200 means HA accepted and parsed the config — not that the
automation is live, not that its entity_id is what you'd guess, and not that
it didn't collide with an existing `id`. So every write does the same
resolve → dispatch → verify → report shape as `cover_command`:

1. POST the config.
2. `GET /api/states`, filter to `automation.*`, and find the entry whose
   `attributes.id == automation_id` — **do not guess the entity_id from the
   alias.** HA slugifies it, collisions get suffixed, and inferring the result
   is exactly the "measure, don't infer" mistake this codebase's docstring
   already warns against for polarity. The `id` attribute is documented to
   exist on every automation entity and is the reliable join key.
3. Report `entity_id`, `state` (`on`/`off`/`unavailable`), and `confirmed`
   (state is `on` when `enabled=True` was requested, `off` when `False`).

```
create_automation("Hallway motion → light", ...)
→ {"ok": true, "automation_id": "hermes_hallway_motion_light",
   "entity_id": "automation.hallway_motion_light", "state": "on",
   "confirmed": true}
```

### 2.5 Ownership and idempotency

Two problems if this is skipped: the agent loses track of what it created,
and re-running "set up a motion light in the hallway" a second time creates a
duplicate instead of updating the first.

- **Deterministic id.** `automation_id` defaults to `hermes_<slug(alias)>` if
  not given. Calling `create_automation` again with the same alias is then a
  natural update, not a duplicate — check via `GET` before `POST` and report
  `created` vs `updated` in the result.
- **Label every automation this server writes** with `hermes-managed`, via
  `config/entity_registry/update` (area/label writes are WebSocket-only, see
  §4) right after the create/update read-back confirms the entity_id. This
  is what makes it possible for `list_automations` to answer "what has this
  agent set up" without a naming-convention guess, and it's visible as a
  label chip in the HA UI too — a human editing it by hand can see it's
  managed elsewhere.
- **`delete_automation` returns the deleted config in full**, not just
  `{"ok": true}`. DESIGN.md §2's "every write surface needs an undo" has no
  literal analogue here — HA has no trash for automations — so the mitigation
  is that the caller (skill) always has enough in hand to recreate it via
  `create_automation` if the deletion was a mistake. State this explicitly in
  the tool's docstring so a skill built on top doesn't discard the response.

### 2.6 `list_automations` / `get_automation`

`list_automations(managed_only: bool = True)` — default to the
`hermes-managed` label so the curated-surface rule from DESIGN.md §2 applies
here too: an agent asked to "clean up the automations" should not casually
enumerate and reason about a decade of hand-written ones it doesn't
understand. `managed_only=False` lists everything, read-only, for the rare
audit case — with the storage-vs-package caveat from §2.1 attached to each
entry that isn't editable through this API.

`get_automation(automation_id)` returns the structured Trigger/Condition/
Action shape (§2.2 in reverse), not raw HA YAML — an agent reasoning about
"what does this automation currently do" should see the same vocabulary it
writes in, not have to re-parse HA's wire format.

---

## 3. Safety: action domains are allow-listed, forever

[`../hass-mcp/FUTURE.md`](../hass-mcp/FUTURE.md) §1 establishes the rule for
this whole repo: locks, the alarm, and the garage are **absent from
`tools/list`**, not present-and-refusing, because untrusted text (email,
calendar invites) reaches the same context that calls tools, and a tool the
agent can see is a tool it will eventually be talked into trying.

An automation's `action` block is a second path to the exact same risk, and a
worse one — a single saved automation is standing infrastructure, not a
one-shot call, so a bad action step keeps firing after the conversation that
created it is long gone. So `create_automation`/`update_automation` must
enforce the identical tier at build time:

- **Allowed action kinds:** `light`, `cover`, `scene`, `delay` — exactly the
  domains this server already exposes for live control, nothing more. No
  `notify`, no `script.turn_on` (a script can contain anything), no raw
  `action: <domain>.<service>` escape hatch.
- **Never emit `lock.*`, `alarm_control_panel.*`, `cover.` on a garage-class
  device, or any service this server does not itself expose live.** This
  should be a hard check inside the action builder, not a docstring
  admonition — reject at validation time with a `permanent: true` error, the
  same shape `light_command` already uses for capability refusals.
- **Climate is out of scope for now** because `ha-mcp` (unlike `hass-mcp`)
  has no climate tools yet — see the two-servers note in §6. Do not add a
  climate action kind until `ha-mcp` has a live `climate_command` to mirror
  its capability checks against; building the automation path first and the
  live path never is how this server ends up less safe than the one it's
  meant to be an upgrade from.

If a future need genuinely requires a wider action vocabulary (e.g. `notify`
for "text me when the driveway sensor trips"), that is a new tier decision to
make deliberately, the way §1 of FUTURE.md made it for locks — not a default
this feature should ship with.

---

## 4. WebSocket transport for the registries

### 4.1 Auth and envelope

`GET /api/websocket` (protocol upgrade) → server sends `{"type":
"auth_required", ...}` → client sends `{"type": "auth", "access_token":
TOKEN}` → server sends `{"type": "auth_ok", ...}` or `auth_invalid` (then
closes). After auth, every command is `{"id": <int>, "type": "<command>",
...fields}` and every response is `{"id": <int>, "type": "result", "success":
bool, "result": <data> | null, "error": {"code": ..., "message": ...} |
null}`. `id` must increase monotonically per connection and is how responses
are matched to requests — this is the correlation the choke point below
exists to hide.
([developers.home-assistant.io/docs/api/websocket](https://developers.home-assistant.io/docs/api/websocket/))

### 4.2 `_ws_call()` — the second choke point

Mirrors `_req()` exactly: same `{"ok": bool, ...}` / `_fail(msg, ...)` return
shape, so every tool built on top of it fails exactly as legibly as the REST
ones do. Short-lived connection per call is fine at this server's call
volume (area edits are rare, unlike light commands) and avoids keeping
long-lived state in a stdio MCP server process.

```python
async def _ws_call(msg_type: str, **fields: Any) -> dict[str, Any]:
    """Single WebSocket choke point - open, auth, send one command, close.

    Mirrors _req(): {"ok": True, "data": ...} or _fail(msg, ...). Never
    raises past this boundary.
    """
    if not TOKEN:
        return _fail(TOKEN_HELP)
    ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    try:
        async with websockets.connect(ws_url, open_timeout=TIMEOUT) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                return _fail("unexpected WebSocket handshake from Home Assistant")
            await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                return _fail("401 unauthorized - HASS_TOKEN is invalid or expired")
            await ws.send(json.dumps({"id": 1, "type": msg_type, **fields}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=TIMEOUT))
    except (OSError, TimeoutError) as e:
        return _fail(f"cannot reach Home Assistant WebSocket API: {type(e).__name__}")
    if not reply.get("success"):
        err = reply.get("error") or {}
        return _fail(err.get("message") or "WebSocket command failed", code=err.get("code"))
    return {"ok": True, "data": reply.get("result")}
```

`mcp.tool()` handlers in the SDK this server uses are synchronous; wrap calls
to `_ws_call` with the same sync bridge already implicit in `_req()`'s use of
`httpx.Client` (i.e. `asyncio.run(_ws_call(...))` per call, or switch the
handful of area tools to `async def` if the SDK version in use supports async
tool handlers — confirm against the installed `mcp>=2.0.0` before choosing).

### 4.3 Commands used

| Tool | WS command(s) |
| --- | --- |
| `list_areas` | `config/area_registry/list` |
| `create_area` | `config/area_registry/create` (`name`, optional `aliases`) |
| `assign_area` | `config/entity_registry/update` (`entity_id`, `area_id`) — or `config/device_registry/update` when assigning a whole device; see 5.2 |

Field names for `area_registry/create|update`: `name`, `aliases`, `floor_id`,
`icon`, `labels`, `picture`. For `entity_registry/update`: `entity_id`,
`area_id`, `aliases`, `labels`, `name`, plus the ones this feature doesn't
need (`disabled_by`, `hidden_by`, `new_entity_id`, ...) — pass only what's
supplied, never null out fields the caller didn't mention.

---

## 5. Areas

### 5.1 Tools

```python
@mcp.tool()
def list_areas() -> dict[str, Any]:
    """List every area: id, name, aliases. Discovery tool - call before
    assign_area so a real area_id is used, never invented."""

@mcp.tool()
def create_area(name: str, aliases: list[str] | None = None) -> dict[str, Any]:
    """Create a new area/room. Returns the new area_id.
    Refuses if an area with this name (or a matching alias) already exists -
    call list_areas first and use assign_area if the room is already there."""

@mcp.tool()
def assign_area(entity_id: str, area: str) -> dict[str, Any]:
    """Move one entity into an area. `area` is matched fuzzily against
    list_areas() the same way resolve() matches devices - name or alias,
    case-insensitive. Ambiguous matches are refused with the candidate list,
    never guessed."""
```

### 5.2 Device-level vs entity-level area

Home Assistant has two places an area can be set: the **device**
(`config/device_registry/update`, field `area_id`) and the **entity**
(`config/entity_registry/update`, field `area_id`). An entity with no
explicit area inherits its device's area; an explicit entity-level area
overrides it. This is exactly the kind of polarity/precedence fact §2's
"measure, don't infer" rule exists for — `assign_area` should default to
setting the entity-level override (matches "the theater lamp is in the
theater" regardless of what device it's paired with) but its docstring must
say so explicitly, and the result should report which registry was actually
written and what the entity's *effective* area now is, not just that the
call succeeded.

### 5.3 Read-back

Areas have no "did it work" ambiguity the way a bulb does — there's no
hardware between the API call and the fact — but the same discipline still
applies: after `assign_area`, re-read the entity via
`config/entity_registry/list` (or the template trick this server already
uses in `_areas()`) and confirm the area actually changed before reporting
success, rather than trusting the WS `success: true` at face value. `_req()`'s
own comment block already states the house rule: *"Never claim a device
changed without reading state back."* Registries are not exempt just because
they're metadata instead of hardware.

---

## 6. Two Home Assistant servers exist — this affects rollout

This repo currently has **two** HA integrations: `ha-mcp` (this one — no
`house.json`, resolves live via `resolve()`/`_entities()`, MCP SDK 2.0) and
`hass-mcp` (older, `mcpkit`-based, curated via `house.json`, has climate
tools this server lacks, and is what
[`skills/home-control/SKILL.md`](../skills/home-control/SKILL.md) currently
points at). This design doc is scoped to `ha-mcp` only, per the request that
started it, but the implementing agent should not silently let the two
diverge further:

- Do **not** port this feature to `hass-mcp` as a side effect of building it
  here — that server's map-based model doesn't fit the "any entity_id can be
  a trigger target" requirement of §2.3 without its own design pass.
- Do flag, when this ships, that `skills/home-control/SKILL.md` will need
  either a new skill for automation/area work pointed at `ha`, or an update
  clarifying which server owns what — an agent told "everything goes through
  `hass`" and then handed `ha`'s new tools will be confused about which one
  to call. This is called out as a known gap, not silently left for someone
  to trip over.

---

## 7. Testing

DESIGN.md §2: *"Test the things that fail quietly."* For this feature, quiet
failure looks like:

- `create_automation` returns 200 from the POST but the entity never actually
  appears in `/api/states` (id collision, storage-file write failure) —
  `confirmed` must be `false`, not inferred `true` from the POST status.
- An automation is created with an action step Home Assistant will accept
  syntactically but that targets a light that doesn't support the requested
  capability — must be refused *before* the POST, by the same capability
  check `light_command` uses, not discovered later when it silently no-ops
  at 6am.
- `assign_area` against a nonexistent or ambiguous area name — must return
  the candidate list, the same shape `resolve()` already returns for
  ambiguous device names, never guess the closest one.
- The `hermes-managed` label write (§2.5) failing silently after a
  successful automation create — the automation would work, but
  `list_automations` would then quietly lose track of it. Should be surfaced
  as a warning in the create response even though `ok: true`, not swallowed.

`ha-mcp` currently has **no test suite** (`tests/test_hass.py` covers
`hass-mcp`, not this server) and **no CLI dispatch** the way `mcpkit`-based
servers get for free — it's built directly on the MCP SDK's `MCPServer` with
no subcommand entry point. DESIGN.md §2 calls CLI parity *"the
highest-leverage convention in the repo... do not build a server without
it."* Given this feature is the highest-stakes write path `ha-mcp` will have
— it authors standing config that runs unattended, not a single reversible
device command — this is the moment to close that gap rather than let a
second server accumulate the same debt. At minimum: a fake-HA test harness
(HTTP + WebSocket, both deaf-by-default per DESIGN.md §2's definition of the
useful failure mode) exercising the confirm/refuse paths above, even if full
CLI subcommand parity is a separate follow-up.

---

## 8. Rollout order

1. **`_ws_call()` + `list_areas`.** Read-only, proves the second transport
   works before anything writes through it.
2. **`create_area` / `assign_area`.** Low risk (metadata, not device state,
   not automations), and immediately useful on its own — the area map is a
   prerequisite for writing area-scoped automations later ("every light in
   the office").
3. **`list_automations` / `get_automation`, read-only.** Same reasoning as
   `hass-mcp`'s own build order in DESIGN.md §9: learn the vocabulary — what
   HA calls things, what's already there, what's storage-editable — before a
   write path depends on assumptions about it.
4. **`create_automation` / `update_automation`, allow-listed actions only**
   (§3), with full read-back (§2.4) and the `hermes-managed` label (§2.5)
   from the first version, not bolted on later.
5. **`automation_command` / `delete_automation`.**
6. **A skill** (or an update to `home-control`) that turns "motion in the
   hallway should turn on the light" into a `create_automation` call — this
   is where judgment lives per DESIGN.md §1: sensible defaults (how long
   should the light stay on after motion stops — `for_seconds` on a
   complementary "no motion" trigger, not hardcoded here), how much to
   confirm before writing standing automation, and how to talk about what
   was created, all belong in prose, not in this server.
