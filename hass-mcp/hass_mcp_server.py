#!/usr/bin/env python3
"""
hass-mcp - Home Assistant, restricted to the things worth saying out loud.

Lights, blinds, thermostats, scenes. Deliberately **not** locks, alarms, door
sensors, or history - see FUTURE.md for why those are a separate problem and
what they need before they are safe to add.

Three things make this different from a REST wrapper:

  1. The agent never sees an entity id. It says "office"; the map in HASS_MAP
     turns that into light.office_ceiling. Swap a bulb, edit one line, and
     every prior conversation still works.

  2. The tool surface is verbs, not a service dispatcher. set_cover(room,
     position_pct) rather than call_service(domain, service, data). A generic
     dispatcher re-imports every guess the map just eliminated.

  3. Every write is read back. Home Assistant returns 200 when a service call
     is *dispatched*, not when anything happened - a bulb switched off at the
     wall and a working bulb are indistinguishable from the response. So we
     poll the entity until it matches, and report what is actually true. This
     is the whole reason the server exists.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python hass_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python hass_mcp_server.py hass_status
         python hass_mcp_server.py home_status
         python hass_mcp_server.py set_lights room=office state=on brightness_pct=40

Environment:
    HASS_URL      e.g. http://homeassistant.local:8123. From inside Docker this
                  must be a LAN address; localhost is the container.
    HASS_TOKEN    long-lived access token. Never read from disk.
    HASS_MAP      path to the room map. Defaults to house.json beside this file.
    HASS_TIMEOUT  seconds for a single HTTP call. Default 10.

No dependencies. urllib and the standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

HASS_URL = os.environ.get("HASS_URL", "http://homeassistant.local:8123").rstrip("/")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
# Defaults to ~/.hermes, beside Hermes' own config, and deliberately not to the
# git checkout: the map holds your real entity ids and a `git pull` would
# overwrite it.
HASS_MAP = os.environ.get("HASS_MAP") or os.path.join(
    os.path.expanduser("~"), ".hermes", "house.json")
HASS_TIMEOUT = int(os.environ.get("HASS_TIMEOUT", "10"))

# How long to wait for reality to catch up, per domain. These are not arbitrary:
# a light confirms in well under a second, a blind physically travels for the
# better part of half a minute, and a thermostat never reaches setpoint at all -
# so climate confirms that the *setpoint* changed and nothing more.
CONFIRM_TIMEOUT = {"light": 6, "cover": 30, "climate": 8, "scene": 6}
POLL_INTERVAL = 0.4

# Home Assistant cover feature bits. A cover without SET_POSITION can only be
# told open or shut, and asking it for 40% fails in a way worth explaining.
COVER_OPEN, COVER_CLOSE, COVER_SET_POSITION = 1, 2, 4


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def api(path, payload=None, method=None):
    if not HASS_TOKEN:
        raise ToolError(
            "HASS_TOKEN is not set. Create a long-lived access token in Home "
            "Assistant under your profile, and put it in the MCP config for "
            "this server."
        )
    url = f"{HASS_URL}/api/{path.lstrip('/')}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method or ("POST" if payload is not None else "GET"),
        headers={"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HASS_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 401:
            raise ToolError("Home Assistant rejected HASS_TOKEN (401). The token is wrong or was revoked.")
        if exc.code == 404:
            raise ToolError(
                f"Home Assistant has no endpoint {path!r} (404). If this is a state "
                "lookup, the entity id in HASS_MAP does not exist - run hass_status, "
                "which lists every mapped entity Home Assistant does not know about."
            )
        raise ToolError(f"Home Assistant returned HTTP {exc.code} for {path}: {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Could not reach Home Assistant at {HASS_URL}: {exc.reason}. "
            "Inside Docker this must be a LAN address - 'localhost' is the "
            "container, not the host. Check from inside the container first."
        )


def get_state(entity_id):
    try:
        return api(f"states/{entity_id}")
    except ToolError:
        return None


def all_states():
    return {row["entity_id"]: row for row in api("states")}


def call_service(domain, service, data):
    return api(f"services/{domain}/{service}", payload=data)


# ---------------------------------------------------------------------------
# The map - the semantic layer
#
# This file is the whole point. Everything the agent says is a room name and
# everything Home Assistant understands is an entity id, and this is the only
# place the two meet.
# ---------------------------------------------------------------------------

_map_cache = None
_map_mtime = None


def house():
    """Load the map, reloading whenever the file has changed on disk.

    Adding a device is editing this file, and it happens often enough that
    needing to restart the server for every new bulb would be a real tax - and
    a silent one, because the old map keeps working and the new entity is just
    mysteriously absent. Checking the mtime costs one stat per call.
    """
    global _map_cache, _map_mtime
    mtime = os.path.getmtime(HASS_MAP) if os.path.isfile(HASS_MAP) else None
    if _map_cache is not None and mtime == _map_mtime:
        return _map_cache
    if not os.path.isfile(HASS_MAP):
        raise ToolError(
            f"No room map at {HASS_MAP}. Copy house.example.json to house.json "
            "and fill in your own entity ids. Without it this server has no way "
            "to turn 'office' into an entity, and it will not guess.",
        )
    try:
        with open(HASS_MAP, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ToolError(f"{HASS_MAP} is not valid JSON: {exc}")
    if not isinstance(data.get("rooms"), dict) or not data["rooms"]:
        raise ToolError(f"{HASS_MAP} has no 'rooms' object. Nothing can be addressed.")
    _map_cache, _map_mtime = data, mtime
    return data


def mapped_entities():
    """Every entity id the map names, in one flat set."""
    data = house()
    found = set()
    for cfg in data["rooms"].values():
        for kind in ("lights", "covers", "climate", "sensors"):
            found.update(as_list(cfg.get(kind)))
    found.update(as_list(list((data.get("scenes") or {}).values())))
    return found


def as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def resolve_room(name, needs=None):
    """Exact, then alias, then prefix, then substring - case-insensitive.

    A name that matches a room which lacks the requested capability returns
    *that* reason, not "no such room". "The office has no blinds" ends the
    conversation; "unknown room" sends the agent hunting through synonyms.
    """
    rooms = house()["rooms"]
    if not name or not str(name).strip():
        raise ToolError("No room named.", known_rooms=sorted(rooms))
    want = str(name).strip().lower()

    def entry(key):
        return key, rooms[key]

    for key in rooms:
        if key.lower() == want:
            return check(entry(key), needs)
    for key, cfg in rooms.items():
        if want in [a.strip().lower() for a in as_list(cfg.get("aliases"))]:
            return check(entry(key), needs)
    matches = [k for k in rooms if k.lower().startswith(want)] or \
              [k for k in rooms if want in k.lower()]
    if len(matches) == 1:
        return check(entry(matches[0]), needs)
    if len(matches) > 1:
        raise ToolError(f"{name!r} matches more than one room. Say which.", candidates=sorted(matches))
    raise ToolError(
        f"No room named {name!r}. Add it to {os.path.basename(HASS_MAP)} if it is real.",
        known_rooms=sorted(rooms),
    )


def check(pair, needs):
    key, cfg = pair
    if needs and not as_list(cfg.get(needs)):
        having = sorted(k for k, c in house()["rooms"].items() if as_list(c.get(needs)))
        raise ToolError(
            f"The {key} has no {needs} in the map. This is final unless you add "
            f"them to {os.path.basename(HASS_MAP)} - do not try another room or "
            "another approach.",
            rooms_with=having,
        )
    return key, cfg


# ---------------------------------------------------------------------------
# Read-back verification
#
# The single most important function here. See the module docstring.
# ---------------------------------------------------------------------------


def confirm(entity_id, predicate, timeout):
    """Poll until the entity matches, or give up. Returns (matched, state)."""
    deadline = time.monotonic() + timeout
    state = None
    while True:
        state = get_state(entity_id)
        if state and predicate(state):
            return True, state
        if time.monotonic() >= deadline:
            return False, state
        time.sleep(POLL_INTERVAL)


def why_not(entity_id, state):
    """A sentence explaining a device that did not do as it was told."""
    if state is None:
        return f"{entity_id} does not exist in Home Assistant - the map is wrong"
    if state.get("state") == "unavailable":
        return f"{entity_id} is unavailable (off at the switch, or off the mesh)"
    if state.get("state") == "unknown":
        return f"{entity_id} reports unknown - it is paired but not responding"
    return f"{entity_id} is still {state.get('state')!r}"


def apply_and_confirm(entity_ids, domain, service, data, predicate, label):
    """Dispatch to every entity, then verify each. Partial results are the norm.

    Rooms are groups, and a group with one dead member is the most common real
    state in a house. Reporting "kitchen lights on" when one of four is missing
    is how you train someone to stop trusting the assistant.
    """
    call_service(domain, service, dict(data, entity_id=entity_ids))
    confirmed, failed = [], []
    for entity_id in entity_ids:
        matched, state = confirm(entity_id, predicate, CONFIRM_TIMEOUT.get(domain, 6))
        (confirmed if matched else failed).append(entity_id if matched else why_not(entity_id, state))
    if not failed:
        return {"ok": True, "summary": f"{label} (confirmed)", "confirmed": confirmed}
    if not confirmed:
        return {
            "ok": False,
            "error": f"{label} — command accepted but nothing changed. " + "; ".join(failed),
            "confirmed": [], "failed": failed,
        }
    return {
        "ok": True,
        "summary": f"{label} — {len(confirmed)} of {len(entity_ids)} confirmed. " + "; ".join(failed),
        "partial": True, "confirmed": confirmed, "failed": failed,
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@tool("Check the connection, the token, and the room map. Run this first when anything looks wrong — it names mapped entities Home Assistant does not have.")
def hass_status():
    config = api("config")
    states = all_states()
    data = house()
    mapped, missing, unavailable = [], [], []
    for room, cfg in data["rooms"].items():
        for kind in ("lights", "covers", "climate", "sensors"):
            for entity_id in as_list(cfg.get(kind)):
                mapped.append(entity_id)
                if entity_id not in states:
                    missing.append(f"{entity_id} (mapped to {room}.{kind})")
                elif states[entity_id].get("state") == "unavailable":
                    unavailable.append(entity_id)
    for scene in as_list(list(data.get("scenes", {}).values())):
        mapped.append(scene)
        if scene not in states:
            missing.append(f"{scene} (mapped as a scene)")

    summary = (f"Home Assistant {config.get('version', '?')} at {HASS_URL}. "
               f"{len(data['rooms'])} rooms, {len(mapped)} mapped entities.")
    if missing:
        summary += f" {len(missing)} mapped entities DO NOT EXIST — fix the map."
    if unavailable:
        summary += f" {len(unavailable)} unavailable right now."
    return {
        "ok": not missing, "summary": summary,
        "version": config.get("version"), "map": HASS_MAP,
        "rooms": sorted(data["rooms"]), "mapped_entities": len(mapped),
        "missing_entities": missing, "unavailable_entities": unavailable,
        "temperature_unit": (config.get("unit_system") or {}).get("temperature", "?"),
        "error": None if not missing else "Some mapped entities do not exist in Home Assistant.",
    }


@tool("List the rooms this server can address, what each one has, and the other names each answers to. Use this when a room name is rejected.")
def list_rooms():
    data = house()
    rows = []
    for room, cfg in sorted(data["rooms"].items()):
        has = [k for k in ("lights", "covers", "climate", "sensors") if as_list(cfg.get(k))]
        rows.append({"room": room, "has": has, "aliases": as_list(cfg.get("aliases"))})
    scenes = sorted(data.get("scenes", {}))
    parts = [f"{r['room']} ({', '.join(r['has']) or 'nothing mapped'}"
             + (f"; also '{', '.join(r['aliases'])}'" if r["aliases"] else "") + ")" for r in rows]
    return {"ok": True,
            "summary": "Rooms: " + "; ".join(parts) + (f". Scenes: {', '.join(scenes)}." if scenes else ""),
            "rooms": rows, "scenes": scenes}


# Which map key each Home Assistant domain belongs under. Domains absent from
# here are not addressable by this server, which is a scope decision rather than
# an oversight - see FUTURE.md.
DOMAIN_TO_KEY = {"light": "lights", "cover": "covers", "climate": "climate",
                 "sensor": "sensors", "scene": "scenes"}
DEFERRED_DOMAINS = {
    "lock": "locks are deferred - see FUTURE.md",
    "alarm_control_panel": "the alarm is deferred - see FUTURE.md",
    "binary_sensor": "door and window contacts are deferred - see FUTURE.md",
}


@tool(
    "List what Home Assistant actually has, so a new device can be added to the "
    "map without guessing at its entity id. Defaults to the things this server "
    "can address that are not mapped yet — which is exactly the question you "
    "have after plugging something in. Read-only.",
    {"domain": s("One of light, cover, climate, scene, sensor. Omit for all the addressable ones.",
                 enum=["light", "cover", "climate", "scene", "sensor"]),
     "unmapped_only": b("Hide entities already in the map.", default=True),
     "search": s("Substring filter on entity id or friendly name.")},
)
def discover_entities(domain=None, unmapped_only=True, search=None):
    if domain in DEFERRED_DOMAINS:
        raise ToolError(
            f"This server does not address the {domain!r} domain: "
            f"{DEFERRED_DOMAINS[domain]}. Listing them here would only invite "
            "an attempt to use them.",
            addressable_domains=["light", "cover", "climate", "scene", "sensor"],
        )
    wanted = [domain] if domain else ["light", "cover", "climate", "scene"]
    known = mapped_entities()
    states = all_states()
    needle = (search or "").strip().lower()

    found = {}
    for entity_id, st in sorted(states.items()):
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain not in wanted:
            continue
        if unmapped_only and entity_id in known:
            continue
        name = (st.get("attributes") or {}).get("friendly_name", "")
        if needle and needle not in entity_id.lower() and needle not in name.lower():
            continue
        found.setdefault(entity_domain, []).append({
            "entity_id": entity_id, "name": name, "state": st.get("state"),
        })

    if not found:
        scope = "unmapped " if unmapped_only else ""
        return {"ok": True,
                "summary": f"No {scope}entities in {', '.join(wanted)}."
                           + (" Everything is already in the map." if unmapped_only else ""),
                "entities": {}}

    # The point of this tool is the line you paste into house.json, not the
    # listing. Hand it over ready to use.
    snippet = {DOMAIN_TO_KEY[d]: [e["entity_id"] for e in items] for d, items in found.items()}
    lines = []
    for entity_domain, items in found.items():
        lines.append(f"{entity_domain}: " + ", ".join(
            f"{e['entity_id']}" + (f" ({e['name']})" if e["name"] else "") for e in items))

    total = sum(len(v) for v in found.values())
    return {
        "ok": True,
        "summary": f"{total} entit{'y' if total == 1 else 'ies'} not in the map:\n  " + "\n  ".join(lines)
                   + f"\n\nTo add them to a room in {os.path.basename(HASS_MAP)}:\n"
                   + json.dumps(snippet, indent=2),
        "entities": found,
        "map_snippet": snippet,
        "map_file": HASS_MAP,
        "note": "Paste the snippet into the right room block. The map reloads on "
                "its own when the file changes — no restart needed.",
    }


def describe_room(room, cfg, states):
    """One room, as a short phrase. Silence means nothing interesting."""
    bits, problems = [], []
    on = []
    for entity_id in as_list(cfg.get("lights")):
        st = states.get(entity_id)
        if st is None:
            problems.append(f"{entity_id} missing from Home Assistant")
        elif st["state"] == "unavailable":
            problems.append(f"{entity_id} unavailable")
        elif st["state"] == "on":
            pct = (st.get("attributes") or {}).get("brightness")
            on.append(f"{round(pct / 2.55)}%" if pct else "on")
    if on:
        bits.append(f"lights on ({', '.join(sorted(set(on)))})")

    positions = []
    for entity_id in as_list(cfg.get("covers")):
        st = states.get(entity_id)
        if st is None:
            problems.append(f"{entity_id} missing from Home Assistant")
            continue
        pos = (st.get("attributes") or {}).get("current_position")
        positions.append(f"{pos}%" if pos is not None else st["state"])
    if positions and set(positions) - {"closed", "0%"}:
        bits.append("covers " + ", ".join(positions))

    for entity_id in as_list(cfg.get("climate")):
        st = states.get(entity_id)
        if st is None:
            continue
        attrs = st.get("attributes") or {}
        current, target = attrs.get("current_temperature"), attrs.get("temperature")
        if current is not None:
            bits.append(f"{current:g}° → {target:g}°" if target is not None else f"{current:g}°")

    for entity_id in as_list(cfg.get("sensors")):
        st = states.get(entity_id)
        if st and st["state"] not in ("unknown", "unavailable"):
            unit = (st.get("attributes") or {}).get("unit_of_measurement", "")
            bits.append(f"{(st.get('attributes') or {}).get('friendly_name', entity_id)} {st['state']}{unit}")

    return bits, problems


@tool(
    "The whole house in one call — what is on, what is open, what is broken. "
    "Use this to answer 'is everything alright' and as the house section of "
    "the daily brief. Anything wrong comes first; a quiet house says little.",
    {"verbose": b("List every room, including the ones with nothing happening.")},
)
def home_status(verbose=False):
    states = all_states()
    data = house()
    lines, problems, batteries = [], [], []

    for entity_id, st in states.items():
        attrs = st.get("attributes") or {}
        if attrs.get("device_class") == "battery" and st["state"].replace(".", "").isdigit():
            if float(st["state"]) < 20:
                batteries.append(f"{attrs.get('friendly_name', entity_id)} {st['state']}%")

    for room, cfg in sorted(data["rooms"].items()):
        bits, room_problems = describe_room(room, cfg, states)
        problems.extend(room_problems)
        if bits:
            lines.append(f"{room}: " + ", ".join(bits))
        elif verbose:
            lines.append(f"{room}: quiet")

    head = "House"
    out = []
    if problems:
        out.append("! " + "; ".join(problems[:6]))
    if batteries:
        out.append("! low battery: " + ", ".join(sorted(batteries)[:6]))
    out.extend(lines)
    if not out:
        return {"ok": True, "summary": "House — everything off and closed, nothing to report.",
                "needs_attention": False, "rooms": []}
    return {
        "ok": True,
        "summary": head + ("" if problems or batteries else " — nothing wrong") + "\n  " + "\n  ".join(out),
        "needs_attention": bool(problems or batteries),
        "problems": problems, "low_batteries": batteries,
    }


@tool(
    "Everything mapped to one room, including what is quiet.",
    {"room": s("Room name or alias.")},
    required=["room"],
)
def room_status(room):
    key, cfg = resolve_room(room)
    states = all_states()
    bits, problems = describe_room(key, cfg, states)
    detail = {kind: {e: (states.get(e) or {}).get("state", "MISSING") for e in as_list(cfg.get(kind))}
              for kind in ("lights", "covers", "climate", "sensors") if as_list(cfg.get(kind))}
    summary = f"{key}: " + (", ".join(bits) if bits else "nothing on, nothing open")
    if problems:
        summary += " — ! " + "; ".join(problems)
    return {"ok": True, "summary": summary, "room": key, "entities": detail, "problems": problems}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@tool(
    "Turn a room's lights on or off, optionally at a brightness. Brightness is "
    "absolute, not relative — there is no 'dim a bit'. To go dimmer, read the "
    "current level with room_status and set a lower number, so the reasoning "
    "is visible instead of guessed.",
    {"room": s("Room name or alias."),
     "state": s("on or off.", enum=["on", "off"]),
     "brightness_pct": i("1-100. Only meaningful with state=on.", minimum=1, maximum=100),
     "color_temp_k": i("Colour temperature in kelvin, roughly 2000 (warm) to 6500 (cold).",
                       minimum=1500, maximum=7000)},
    required=["room", "state"],
)
def set_lights(room, state, brightness_pct=None, color_temp_k=None):
    key, cfg = resolve_room(room, needs="lights")
    entities = as_list(cfg["lights"])
    state = str(state).strip().lower()
    if state not in ("on", "off"):
        raise ToolError(f"state must be 'on' or 'off', got {state!r}.")
    if state == "off" and (brightness_pct or color_temp_k):
        raise ToolError("Brightness and colour temperature mean nothing with state=off. "
                        "Set state=on with a low brightness if you meant to dim.")

    data = {}
    if brightness_pct is not None:
        data["brightness_pct"] = brightness_pct
    if color_temp_k is not None:
        data["color_temp_kelvin"] = color_temp_k

    if state == "on":
        def matches(st):
            if st["state"] != "on":
                return False
            if brightness_pct is None:
                return True
            actual = (st.get("attributes") or {}).get("brightness")
            # Home Assistant stores 0-255, so a requested percentage lands
            # within rounding distance rather than exactly.
            return actual is not None and abs(round(actual / 2.55) - brightness_pct) <= 3
    else:
        def matches(st):
            return st["state"] == "off"

    label = f"{key} lights → {state}"
    if brightness_pct is not None:
        label += f", {brightness_pct}%"
    if color_temp_k is not None:
        label += f", {color_temp_k}K"
    result = apply_and_confirm(entities, "light", f"turn_{state}", data, matches, label)
    result["room"] = key
    return result


@tool(
    "Set a room's blinds to a position. 0 is fully closed, 100 is fully open. "
    "Absolute only. Blinds take time to travel, so this waits for them.",
    {"room": s("Room name or alias."),
     "position_pct": i("0 closed, 100 open.", minimum=0, maximum=100)},
    required=["room", "position_pct"],
)
def set_cover(room, position_pct):
    key, cfg = resolve_room(room, needs="covers")
    entities = as_list(cfg["covers"])

    # A cover that cannot be positioned can still be opened and shut. Failing
    # the whole call for a blind that only knows two states would be wrong;
    # silently rounding 40% to "open" would be worse. So: allow the endpoints,
    # and explain the refusal in between.
    positionable, binary_only = [], []
    for entity_id in entities:
        st = get_state(entity_id)
        features = (st.get("attributes") or {}).get("supported_features", 0) if st else 0
        (positionable if features & COVER_SET_POSITION else binary_only).append(entity_id)

    if binary_only and position_pct not in (0, 100):
        raise ToolError(
            f"{', '.join(binary_only)} can only open and close, not sit at "
            f"{position_pct}%. Use 0 or 100 for this room, or drop those covers "
            "from the request.",
            positionable=positionable,
        )

    results = []
    if positionable:
        results.append(apply_and_confirm(
            positionable, "cover", "set_cover_position", {"position": position_pct},
            lambda st, want=position_pct: abs(((st.get("attributes") or {}).get("current_position") or 0) - want) <= 2,
            f"{key} blinds → {position_pct}%"))
    if binary_only:
        service = "open_cover" if position_pct == 100 else "close_cover"
        want = "open" if position_pct == 100 else "closed"
        results.append(apply_and_confirm(
            binary_only, "cover", service, {}, lambda st, w=want: st["state"] == w,
            f"{key} blinds → {want}"))

    ok = all(r["ok"] for r in results)
    return {
        "ok": ok,
        "summary" if ok else "error": " ".join(r.get("summary") or r.get("error", "") for r in results),
        "room": key,
        "confirmed": [e for r in results for e in r.get("confirmed", [])],
        "failed": [e for r in results for e in r.get("failed", [])],
    }


@tool(
    "Set a thermostat's target temperature. This confirms that the *setpoint* "
    "changed, never that the room reached it — a house takes hours and this "
    "tool takes seconds. Do not read a confirmation here as 'the room is warm'.",
    {"room": s("Room or zone name."),
     "target": i("Target temperature, in whatever unit Home Assistant is configured for."),
     "mode": s("Optional HVAC mode.", enum=["heat", "cool", "heat_cool", "auto", "off"])},
    required=["room", "target"],
)
def set_thermostat(room, target, mode=None):
    key, cfg = resolve_room(room, needs="climate")
    entities = as_list(cfg["climate"])
    if mode:
        call_service("climate", "set_hvac_mode", {"entity_id": entities, "hvac_mode": mode})
    result = apply_and_confirm(
        entities, "climate", "set_temperature", {"temperature": target},
        lambda st: (st.get("attributes") or {}).get("temperature") == target,
        f"{key} setpoint → {target}°" + (f", mode {mode}" if mode else ""))
    current = []
    for entity_id in entities:
        st = get_state(entity_id)
        now = (st.get("attributes") or {}).get("current_temperature") if st else None
        if now is not None:
            current.append(f"{now:g}°")
    if current:
        result["summary"] = result.get("summary", "") + f" Currently {', '.join(current)}."
    result["room"] = key
    return result


@tool(
    "Activate a named scene from the map. Scenes are the right way to express "
    "'wind down' or 'movie night' — one entry in the map, edited without "
    "touching this server.",
    {"scene": s("Scene name as it appears in the map.")},
    required=["scene"],
)
def activate_scene(scene):
    scenes = house().get("scenes") or {}
    if not scenes:
        raise ToolError(f"No scenes defined in {os.path.basename(HASS_MAP)}.")
    want = str(scene).strip().lower()
    match = next((k for k in scenes if k.lower() == want), None) or \
            next((k for k in scenes if want in k.lower()), None)
    if not match:
        raise ToolError(f"No scene named {scene!r}.", known_scenes=sorted(scenes))
    entity_id = scenes[match]
    call_service("scene", "turn_on", {"entity_id": entity_id})
    # A scene's own state is the timestamp it last ran, so "did it fire" is the
    # only question we can answer here. What it *did* is visible in home_status.
    time.sleep(1)
    st = get_state(entity_id)
    if st is None:
        return {"ok": False, "error": f"Scene {match} ({entity_id}) does not exist in Home Assistant."}
    return {"ok": True, "summary": f"Scene '{match}' activated. Call home_status to see what it changed.",
            "scene": match}


def banner():
    return (f"HASS_URL={HASS_URL}  HASS_TOKEN={'set' if HASS_TOKEN else 'MISSING'}  "
            f"HASS_MAP={HASS_MAP}{'' if os.path.isfile(HASS_MAP) else ' (NOT FOUND)'}")


if __name__ == "__main__":
    run("hass-mcp", "1.0", banner)
