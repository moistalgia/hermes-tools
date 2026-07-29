# hass-mcp

Home Assistant, restricted to the things worth saying out loud: **lights,
blinds, thermostats, scenes.**

Not locks, not the alarm, not door sensors, not history. Those are deferred on
purpose and [FUTURE.md](FUTURE.md) says what each one needs before it is worth
adding.

No dependencies — `urllib` and the standard library. The protocol half lives in
[../mcpkit.py](../mcpkit.py).

## Why not the existing options

| Option | Why not |
| --- | --- |
| Home Assistant's built-in MCP Server integration | Exposes Assist intents. Narrow, fuzzy, and a failure gives you "Sorry, I didn't understand" with nothing to debug. |
| Third-party REST wrappers | Expose `get_states` and `call_service` over the whole instance. The agent has to guess `light.hue_color_lamp_3` from "the office lamp", guess the service name, and gets `200 OK` whether or not anything happened. |

That last point is the one that matters. **Home Assistant returns 200 when a
service call is dispatched, not when it worked.** A bulb switched off at the
wall, a Z-Wave device with a dead battery, and a perfectly working light are
indistinguishable from the response. Every generic integration reports success
for actions that did not occur, which is exactly why they feel unreliable.

## Install

Nothing to install — no dependencies. Put a map where the server expects it:

```bash
cp house.example.json "$USERPROFILE/.hermes/house.json"
```

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `HASS_URL` | `http://homeassistant.local:8123` | The LAN address of your Home Assistant. |
| `HASS_TOKEN` | *(required)* | A long-lived access token from your Home Assistant profile page. Never read from disk. |
| `HASS_MAP` | `%USERPROFILE%\.hermes\house.json` | The semantic layer. See below. Deliberately not in the checkout — it holds your real entity ids and `git pull` would overwrite it. |
| `HASS_TIMEOUT` | `10` | Seconds for one HTTP call. Separate from confirmation waits. |

## The map is the whole design

[house.example.json](house.example.json) is the only place a spoken room name
meets an entity id. Nothing else in the system — no skill, no other server, and
never the agent — knows that the office ceiling light is
`light.office_ceiling`.

```json
"office": {
  "aliases": ["study", "back room"],
  "lights": ["light.office_ceiling", "light.office_lamp"],
  "covers": ["cover.office_blind"],
  "climate": ["climate.upstairs"]
}
```

Three rules keep it working:

**Room keys are what you say out loud,** and every other name you use goes in
`aliases`. A room you call two things is normal. A room the agent rejects
because you used the other name is a bug in this file.

**Map only what you would ever address by voice.** Forty entities is a working
system; four hundred is a system that guesses. An entity the agent cannot see is
an entity it cannot get wrong.

**When you swap a bulb, edit one line here.** Every past conversation keeps
working, because none of them ever named the old entity. This is the reason the
indirection exists.

Run `hass_status` after every edit — it lists every entity named in the map that
Home Assistant does not actually have, which is the usual mistake.

**The map reloads on its own.** The server watches its modification time and
picks up changes on the next call, so adding a device does not need a restart.
That matters because adding a device is *only* editing this file, and it happens
often enough that a restart per bulb would be a real tax — a silent one, since
the old map keeps working and the new entity is just mysteriously absent.

## Adding a device

The whole workflow, for a light, a blind, a thermostat, or a temperature sensor:

**1. Ask what Home Assistant has that you have not mapped.**

```bash
python hass_mcp_server.py discover_entities
```

```
2 entities not in the map:
  light: light.garage_strip (Garage strip), light.porch (Porch light)

To add them to a room in house.json:
{
  "lights": ["light.garage_strip", "light.porch"]
}
```

**2. Paste the ids into the right room block** in `house.json`, or make a new
room. Narrow the search first with `domain=light` or `search=porch` when the
list is long.

**3. There is no step three.** The map reloads itself, so the next call sees the
new device. Read-back verification covers it automatically, because it verifies
whatever ids it was handed.

Replacing a bulb is the same thing with the id changed in place — and every past
conversation still works, because none of them ever named the old entity. That
is what the indirection is for.

**No code change is ever required to add a device.** If you find yourself editing
`hass_mcp_server.py` to add one, something has gone wrong.

Temperature and humidity sensors already work this way: `sensors` is in the
schema and both `home_status` and `room_status` read it. Battery levels need no
mapping at all — `home_status` reports anything below 20% wherever it lives.
Door and window contacts are the exception and are deferred; `discover_entities`
refuses those domains outright rather than listing things that cannot be used.

## Tools

| Need | Tool |
| --- | --- |
| Is anything wrong? | `hass_status` |
| **What did I just plug in?** | `discover_entities` |
| What can I address? | `list_rooms` |
| **Is the house alright?** | `home_status` |
| What is going on in one room | `room_status` |
| Lights on/off/dimmed | `set_lights` |
| Blinds to a position | `set_cover` |
| Thermostat setpoint | `set_thermostat` |
| A named scene | `activate_scene` |

Verbs, not a service dispatcher. There is deliberately no `call_service` — a
generic dispatcher re-imports every guess the map just eliminated.

`discover_entities` is the only tool that shows raw entity ids, and it is the
one place that is correct: its entire job is handing you an id to paste into the
map. It is read-only and it never acts on what it finds.

## Behaviour worth knowing

**Every write is read back.** After dispatching, the server polls the entity
until its state matches the request or the timeout expires, then reports what is
*actually* true:

```
office lights → on, 40% (confirmed)
kitchen lights → on, 60% — 1 of 2 confirmed.
  light.kitchen_under_cabinet is unavailable (off at the switch, or off the mesh)
```

Partial results are the normal case, not an edge case. Rooms are groups and a
group with one dead member is what a real house looks like. Reporting "kitchen
lights on" when one of four is missing is how you train someone to stop trusting
the assistant.

**Confirmation waits are per-domain** and are not arbitrary: a light confirms in
under a second, a blind physically travels for the better part of half a minute,
and a thermostat never reaches setpoint at all. **`set_thermostat` confirms the
setpoint changed and nothing else** — a confirmation there never means the room
is warm.

**Brightness and position are absolute.** There is no "dim a bit" and no
`+10`. To go dimmer, read `room_status` and set a lower number. Forcing that
into two visible steps means the reasoning is in the transcript instead of being
guessed at.

**Blinds that cannot be positioned are handled, not hidden.** A cover without
`SET_POSITION` still opens and shuts, so 0 and 100 work and anything between
fails with the reason and the list of covers that *can* be positioned. Silently
rounding 40% to "open" would be worse than refusing.

**A room that exists but lacks the thing asked for says so.** "The kitchen has
no covers in the map" ends the conversation. "Unknown room" sends the agent
hunting through synonyms and trying other rooms.

## Build read-only first

Ship `hass_status`, `list_rooms`, `home_status`, `room_status` and use them for
a week before wiring up a single write tool. The reason is vocabulary: you will
discover that you say "the back room" and Home Assistant calls it
`area.office_2`, and it is much cheaper to learn that before write paths depend
on the alias map.

## What stays in Home Assistant

Anything time-triggered, sensor-triggered, latency-sensitive, or safety-critical
stays a native automation. The agent is the interface and the exception handler,
not the control loop.

| Automation | Agent |
| --- | --- |
| Porch light at sunset | "You're heading out and the upstairs blinds are still open" |
| Away mode on last-person-leaves | "The office has run colder than usual all week" |
| Freeze protection | "Want me to close everything up for the night?" |

If you find yourself writing a polling loop in this server, that is an
automation wearing a disguise. Move it.

## Manual test sequence

Run these in order on the host. Stop at the first failure and read the error.

```bash
export HASS_URL=http://192.168.1.x:8123
export HASS_TOKEN=your-long-lived-token
```

```bash
python hass_mcp_server.py hass_status
```

Fix every entity it reports as missing before going further — the rest of the
sequence will fail in confusing ways otherwise.

```bash
python hass_mcp_server.py discover_entities
```

```bash
python hass_mcp_server.py list_rooms
```

```bash
python hass_mcp_server.py home_status
```

```bash
python hass_mcp_server.py set_lights room=office state=on brightness_pct=40
```

That last one should say `(confirmed)`. If it says the command was accepted but
nothing changed, the read-back is doing its job and the bulb is genuinely not
responding — check the wall switch before changing anything here.

Arguments are `key=value`. Quote values containing spaces. Exit code is 0 on
success, 1 on failure.

## Wire into Hermes

```yaml
mcp_servers:
  hass:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/hass-mcp/hass_mcp_server.py", "serve"]
    env:
      HASS_URL: "http://192.168.1.x:8123"
      HASS_TOKEN: "<long-lived token>"
```

`HASS_MAP` is omitted on purpose — the default under `%USERPROFILE%\.hermes\` is
where it belongs, outside the checkout that `git pull` overwrites.

The skill that drives this server is
[home-control](../skills/home-control/SKILL.md).
