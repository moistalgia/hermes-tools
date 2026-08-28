"""
Home Assistant MCP server for Hermes — covers, lights, scenes, automations, areas.

Runs HOST-SIDE on Windows (spawned by Hermes), so it reaches HA at 127.0.0.1.
Install location: C:/dev/hermes-tools/ha-mcp

Env:
  HASS_URL    default http://127.0.0.1:8123   (HA_URL accepted as alias)
  HASS_TOKEN  long-lived access token         (HA_TOKEN accepted as alias)
  HASS_TIMEOUT seconds, default 15

Design invariants (learned the hard way — do not regress these):
  * Never claim a device changed without reading state back -> `confirmed`
  * Never guess entity_ids                                  -> resolve()/list_*
  * Never send a service the device cannot perform           -> capability checks
  * Never assume polarity or scale                           -> measure, don't infer
  * Position: 0=closed, 100=open. Brightness: percent 0-100. Kelvin for color temp.

Automations authored here are NATIVE Home Assistant automations - HA's own
engine triggers them, not this agent (see ../AUTOMATIONS_DESIGN.md §0). Action
steps are restricted to light/cover/scene/delay - the same domains this server
exposes for live control, nothing more. No locks, alarm, garage, scripts, or
raw service calls, ever - see AUTOMATIONS_DESIGN.md §3.

Areas have no REST API in Home Assistant - registry reads/writes go over the
WebSocket API instead (_ws_call), the second and only other transport here.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from typing import Any

import httpx
import websockets.exceptions as _wsexc
import websockets.sync.client as _wsc
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="ha",
    version="2.1.0",
    instructions=(
        "Home Assistant control for covers, lights, scenes, automations and "
        "areas. Always call a list_* or resolve tool first so real entity_ids "
        "are used - never invent them. Cover position is 0=closed/100=open; "
        "light brightness is percent 0-100; color temperature is in Kelvin. "
        "Never tell the user something changed unless the result has "
        "confirmed=true. create_automation authors a NATIVE Home Assistant "
        "automation that HA itself runs - action steps are restricted to "
        "light/cover/scene/delay, never locks/alarm/garage/scripts."
    ),
)

BASE_URL = (os.environ.get("HASS_URL") or os.environ.get("HA_URL") or "http://127.0.0.1:8123").rstrip("/")
TOKEN = os.environ.get("HASS_TOKEN") or os.environ.get("HA_TOKEN") or ""
TIMEOUT = float(os.environ.get("HASS_TIMEOUT") or os.environ.get("HA_TIMEOUT") or "15")

# CoverEntityFeature bitmask (verified against HA developer docs)
COVER_FEATURES = {
    1: "open", 2: "close", 4: "set_position", 8: "stop",
    16: "open_tilt", 32: "close_tilt", 64: "stop_tilt", 128: "set_tilt_position",
}

MOVING_STATES = {"opening", "closing"}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

TOKEN_HELP = (
    "HASS_TOKEN is not set. Put it in the Hermes secrets file host-side "
    "(~/.hermes/.env) and reference it from the mcp_servers env block."
)


def _fail(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def _req(method: str, path: str, **kw: Any) -> dict[str, Any]:
    """Single HTTP choke point so every tool fails identically and informatively."""
    if not TOKEN:
        return _fail(TOKEN_HELP)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.request(method, f"{BASE_URL}{path}", headers=headers, **kw)
    except httpx.ConnectError:
        return _fail(f"cannot reach Home Assistant at {BASE_URL} - is it running?")
    except httpx.TimeoutException:
        return _fail(f"timed out after {TIMEOUT}s calling {path}")
    except httpx.HTTPError as e:
        return _fail(f"HTTP transport error calling {path}: {type(e).__name__}")
    if r.status_code == 401:
        return _fail("401 unauthorized - HASS_TOKEN is invalid or expired")
    if r.status_code == 404:
        return _fail(f"404 not found: {path} (entity or endpoint does not exist)")
    if r.status_code >= 400:
        return _fail(f"HTTP {r.status_code} from {path}", body=r.text[:300])
    try:
        return {"ok": True, "data": r.json()}
    except ValueError:
        return {"ok": True, "data": r.text}


def _ws_call(msg_type: str, **fields: Any) -> dict[str, Any]:
    """Single WebSocket choke point - open, auth, send one command, close.

    Mirrors _req(): {"ok": True, "data": ...} or _fail(msg, ...), and never
    raises past this boundary. Area/entity/device registry writes have no
    REST equivalent in Home Assistant, so this is the second and only other
    transport in this server. Short-lived connection per call: registry
    edits are rare (unlike light commands), so there is no reason to keep a
    socket open in a stdio MCP server process.
    """
    if not TOKEN:
        return _fail(TOKEN_HELP)
    ws_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    try:
        with _wsc.connect(ws_url, open_timeout=TIMEOUT, close_timeout=TIMEOUT) as ws:
            hello = json.loads(ws.recv(timeout=TIMEOUT))
            if hello.get("type") != "auth_required":
                return _fail("unexpected handshake from Home Assistant's WebSocket API")
            ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
            auth = json.loads(ws.recv(timeout=TIMEOUT))
            if auth.get("type") != "auth_ok":
                return _fail("401 unauthorized - HASS_TOKEN is invalid or expired")
            ws.send(json.dumps({"id": 1, "type": msg_type, **fields}))
            reply = json.loads(ws.recv(timeout=TIMEOUT))
    except TimeoutError:
        return _fail(f"timed out after {TIMEOUT}s calling websocket command {msg_type!r}")
    except OSError as e:
        return _fail(
            f"cannot reach Home Assistant WebSocket API at {ws_url} - is it running? "
            f"({type(e).__name__})"
        )
    except _wsexc.WebSocketException as e:
        return _fail(f"WebSocket transport error calling {msg_type!r}: {type(e).__name__}")
    if not reply.get("success"):
        err = reply.get("error") or {}
        return _fail(err.get("message") or f"websocket command {msg_type!r} failed",
                     code=err.get("code"))
    return {"ok": True, "data": reply.get("result")}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return s or "automation"


def _decode_cover_features(mask: int) -> list[str]:
    return [name for bit, name in COVER_FEATURES.items() if mask & bit]


def _areas(domain: str) -> dict[str, str]:
    """entity_id -> area name for one domain, via a single /api/template call.

    /api/states carries no area information, so this is the cheapest way to get it.
    """
    tpl = (
        "{%% set out = namespace(v=[]) %%}"
        "{%% for e in states.%s %%}"
        "{%% set out.v = out.v + [e.entity_id ~ '|' ~ (area_name(e.entity_id) or '')] %%}"
        "{%% endfor %%}{{ out.v | join(';;') }}"
    ) % domain
    res = _req("POST", "/api/template", json={"template": tpl})
    if not res["ok"]:
        return {}
    mapping: dict[str, str] = {}
    for chunk in str(res["data"]).split(";;"):
        if "|" in chunk:
            eid, area = chunk.split("|", 1)
            mapping[eid.strip()] = area.strip()
    return mapping


def _pct_from_255(v: Any) -> int | None:
    try:
        return round(int(v) / 255 * 100)
    except (TypeError, ValueError):
        return None


def _entities(domain: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Normalized entity list for a domain, with area names attached."""
    res = _req("GET", "/api/states")
    if not res["ok"]:
        return [], res
    areas = _areas(domain)
    out: list[dict[str, Any]] = []
    for s in res["data"]:
        eid = s.get("entity_id", "")
        if not eid.startswith(f"{domain}."):
            continue
        a = s.get("attributes", {}) or {}
        item = {
            "entity_id": eid,
            "name": a.get("friendly_name") or eid,
            "area": areas.get(eid) or None,
            "state": s.get("state"),
        }
        if domain == "cover":
            item.update(
                device_class=a.get("device_class"),
                position=a.get("current_position"),
                tilt_position=a.get("current_tilt_position"),
                supports=_decode_cover_features(int(a.get("supported_features") or 0)),
            )
        elif domain == "light":
            modes = [str(m) for m in (a.get("supported_color_modes") or [])]
            item.update(
                brightness_pct=_pct_from_255(a.get("brightness")),
                color_mode=a.get("color_mode"),
                supported_color_modes=modes,
                dimmable=bool(modes) and modes != ["onoff"],
                supports_color_temp="color_temp" in modes,
                supports_rgb=bool(COLOR_MODES & set(modes)),
                min_kelvin=a.get("min_color_temp_kelvin"),
                max_kelvin=a.get("max_color_temp_kelvin"),
                color_temp_kelvin=a.get("color_temp_kelvin"),
                rgb_color=a.get("rgb_color"),
                effects=a.get("effect_list") or [],
            )
        out.append(item)
    return out, None


def _filter_area(items: list[dict[str, Any]], area: str) -> list[dict[str, Any]]:
    if not area:
        return items
    needle = area.strip().lower()
    return [
        i for i in items
        if needle in (i.get("area") or "").lower() or needle in i["name"].lower()
    ]


def _read(entity_id: str) -> dict[str, Any]:
    res = _req("GET", f"/api/states/{entity_id}")
    if not res["ok"]:
        return res
    d = res["data"] if isinstance(res["data"], dict) else {}
    return {"ok": True, "state": d.get("state"), "attributes": d.get("attributes") or {}}


# ------------------------------------------------------------------ diagnostics


@mcp.tool()
def ha_status() -> dict[str, Any]:
    """Check the Home Assistant connection. Call FIRST whenever anything fails.

    Distinguishes "HA unreachable" from "bad token" from "bad arguments", and
    reports how many covers/lights/scenes are visible.
    """
    res = _req("GET", "/api/config")
    if not res["ok"]:
        return res
    d = res["data"] if isinstance(res["data"], dict) else {}
    states = _req("GET", "/api/states")
    counts: dict[str, int] = {}
    if states["ok"]:
        for s in states["data"]:
            dom = str(s.get("entity_id", "")).split(".")[0]
            if dom in ("cover", "light", "scene", "switch"):
                counts[dom] = counts.get(dom, 0) + 1
    return {
        "ok": True,
        "url": BASE_URL,
        "version": d.get("version"),
        "location": d.get("location_name"),
        "state": d.get("state"),
        "entity_counts": counts,
    }


@mcp.tool()
def resolve(query: str, domain: str = "") -> dict[str, Any]:
    """Turn a spoken/natural name into real entity_ids. Use before acting.

    Handles "the theater curtains", "kitchen lights", "movie night scene".
    Returns ALL plausible matches with scores - if several come back for a
    singular request, ask the user which they meant rather than picking one.

    Args:
        query: what the user called it, e.g. "theater curtain".
        domain: optional filter - "cover", "light", or "scene". Empty searches all three.
    """
    q = query.strip().lower()
    if not q:
        return _fail("query is empty")

    domains = [domain] if domain else ["cover", "light", "scene"]
    pool: list[dict[str, Any]] = []
    for dom in domains:
        items, err = _entities(dom)
        if err:
            return err
        for i in items:
            pool.append({**i, "domain": dom})

    scored = []
    for c in pool:
        hay = f"{c['name']} {c['entity_id']} {c.get('area') or ''}".lower()
        score = 1.0 if q in hay else difflib.SequenceMatcher(None, q, c["name"].lower()).ratio()
        if score >= 0.55:
            scored.append({**c, "match_score": round(score, 3)})
    scored.sort(key=lambda x: -x["match_score"])

    return {
        "ok": True,
        "query": query,
        "match_count": len(scored),
        "matches": scored,
        "note": (
            "no match - call list_covers/list_lights/list_scenes to see everything"
            if not scored
            else "multiple matches; confirm with the user before acting"
            if len(scored) > 1 else None
        ),
    }


@mcp.tool()
def get_state(entity_id: str) -> dict[str, Any]:
    """Read one entity's current state and attributes. Works for any domain.

    Use to verify an action or inspect an entity this server has no tool for yet.

    Args:
        entity_id: exact id, e.g. "cover.theater_curtain".
    """
    r = _read(entity_id)
    if not r["ok"]:
        return r
    attrs = r["attributes"]
    out = {
        "ok": True,
        "entity_id": entity_id,
        "name": attrs.get("friendly_name"),
        "state": r["state"],
        "attributes": attrs,
    }
    if entity_id.startswith("cover."):
        out["supports"] = _decode_cover_features(int(attrs.get("supported_features") or 0))
    if entity_id.startswith("light."):
        out["brightness_pct"] = _pct_from_255(attrs.get("brightness"))
    return out


# ------------------------------------------------------------------ covers


@mcp.tool()
def list_covers(area: str = "") -> dict[str, Any]:
    """List every cover with real entity_id, area, state, position, capabilities.

    ALWAYS call before acting. Check `supports` - do not request set_position on
    a cover whose supports list lacks it.

    Position semantics: 0 = fully CLOSED, 100 = fully OPEN.

    Args:
        area: optional case-insensitive area/room filter, e.g. "theater".
    """
    items, err = _entities("cover")
    if err:
        return err
    items = _filter_area(items, area)
    return {"ok": True, "count": len(items), "covers": items}


@mcp.tool()
def cover_command(
    entity_id: str,
    action: str,
    position: int | None = None,
    settle_seconds: float = 2.5,
) -> dict[str, Any]:
    """Command ONE cover, then read state back and report what ACTUALLY happened.

    Returns `confirmed`. NEVER tell the user a cover moved unless it is true -
    HA returns success for an accepted request even when hardware never moves.

    Position: 0 = fully CLOSED, 100 = fully OPEN. "Close it 70%" means position=30.

    Args:
        entity_id: exact id from list_covers/resolve. Never invent one.
        action: "open" | "close" | "stop" | "position"
        position: required when action="position"; 0-100.
        settle_seconds: pause before re-reading (covers report opening/closing).
    """
    if not entity_id.startswith("cover."):
        return _fail(f"'{entity_id}' is not a cover entity - call resolve() first")
    action = action.strip().lower()
    services = {"open": "open_cover", "close": "close_cover",
                "stop": "stop_cover", "position": "set_cover_position"}
    if action not in services:
        return _fail(f"unknown action '{action}' - use open/close/stop/position")

    covers, err = _entities("cover")
    if err:
        return err
    match = next((c for c in covers if c["entity_id"] == entity_id), None)
    if match is None:
        return _fail(f"cover '{entity_id}' does not exist - call list_covers")

    needed = {"position": "set_position"}.get(action, action)
    if needed not in match["supports"]:
        return _fail(
            f"'{match['name']}' does not support '{needed}'",
            supports=match["supports"], permanent=True,
        )

    payload: dict[str, Any] = {"entity_id": entity_id}
    if action == "position":
        if position is None:
            return _fail("position is required when action='position'")
        if not 0 <= int(position) <= 100:
            return _fail("position must be 0-100 (0=closed, 100=open)")
        payload["position"] = int(position)

    before = {"state": match["state"], "position": match["position"]}
    res = _req("POST", f"/api/services/cover/{services[action]}", json=payload)
    if not res["ok"]:
        return res

    time.sleep(max(0.0, settle_seconds))
    after_r = _read(entity_id)
    if not after_r["ok"]:
        return {"ok": True, "entity_id": entity_id, "requested": action,
                "confirmed": False, "note": "command accepted but state read-back failed"}
    after = {"state": after_r["state"],
             "position": after_r["attributes"].get("current_position")}

    if action == "position":
        confirmed = after["position"] == int(position)
    elif action == "open":
        confirmed = after["state"] == "open" or (after["position"] or 0) > (before["position"] or 0)
    elif action == "close":
        confirmed = after["state"] == "closed" or (after["position"] or 100) < (before["position"] or 100)
    else:
        confirmed = after["state"] not in MOVING_STATES

    note = None
    if after["state"] in MOVING_STATES:
        note, confirmed = f"still {after['state']} - re-read state in a few seconds", False
    elif not confirmed:
        note = (
            "request accepted but no state change observed. Possible causes: device "
            "unreachable, already at target, or this entity is polarity-inverted. "
            "Ask the user what physically happened - do not assume."
        )

    return {"ok": True, "entity_id": entity_id, "name": match["name"],
            "requested": action,
            "requested_position": int(position) if action == "position" else None,
            "before": before, "after": after, "confirmed": confirmed, "note": note}


@mcp.tool()
def cover_group(entity_ids: list[str], action: str, position: int | None = None) -> dict[str, Any]:
    """Command SEVERAL covers, reporting each result separately.

    Use for "close all the theater curtains". Get ids from resolve()/list_covers()
    first. Partial failure is never hidden behind an overall success.

    Args:
        entity_ids: exact ids. Never invent them.
        action: "open" | "close" | "stop" | "position"
        position: required when action="position"; 0=closed, 100=open.
    """
    if not entity_ids:
        return _fail("entity_ids is empty - call resolve() or list_covers() first")
    results = [cover_command(entity_id=e, action=action, position=position) for e in entity_ids]
    ok = [r for r in results if r.get("confirmed")]
    return {"ok": True, "requested": action, "total": len(results),
            "confirmed_count": len(ok), "all_confirmed": len(ok) == len(results),
            "results": results}


# ------------------------------------------------------------------ lights


@mcp.tool()
def list_lights(area: str = "") -> dict[str, Any]:
    """List every light with real entity_id, area, state, brightness, capabilities.

    ALWAYS call before acting. Respect the capability flags: `dimmable`,
    `supports_color_temp`, `supports_rgb`, and the `min_kelvin`/`max_kelvin` range.

    This covers Hue bulbs too - they arrive as normal light.* entities through
    Home Assistant, so no separate Hue integration or CLI is needed.

    Args:
        area: optional case-insensitive area/room filter, e.g. "kitchen".
    """
    items, err = _entities("light")
    if err:
        return err
    items = _filter_area(items, area)
    return {"ok": True, "count": len(items), "lights": items}


@mcp.tool()
def light_command(
    entity_id: str,
    action: str = "on",
    brightness_pct: int | None = None,
    color_temp_kelvin: int | None = None,
    rgb_color: list[int] | None = None,
    transition: float | None = None,
    settle_seconds: float = 1.5,
) -> dict[str, Any]:
    """Command ONE light, then read state back and report what ACTUALLY happened.

    Returns `confirmed`. Never tell the user a light changed unless it is true.

    Brightness is PERCENT (0-100), not 0-255. Color temperature is KELVIN
    (mired support was removed from HA in 2026.3). Pass either color_temp_kelvin
    or rgb_color, not both.

    Args:
        entity_id: exact id from list_lights/resolve. Never invent one.
        action: "on" | "off" | "toggle"
        brightness_pct: 0-100. Requires a dimmable light.
        color_temp_kelvin: e.g. 2700 warm, 4000 neutral, 6500 cool. Clamped to
            the light's min_kelvin/max_kelvin.
        rgb_color: [r, g, b] each 0-255. Requires a color-capable light.
        transition: fade duration in seconds.
        settle_seconds: pause before re-reading state.
    """
    if not entity_id.startswith("light."):
        return _fail(f"'{entity_id}' is not a light entity - call resolve() first")
    action = action.strip().lower()
    if action not in ("on", "off", "toggle"):
        return _fail(f"unknown action '{action}' - use on/off/toggle")

    lights, err = _entities("light")
    if err:
        return err
    match = next((l for l in lights if l["entity_id"] == entity_id), None)
    if match is None:
        return _fail(f"light '{entity_id}' does not exist - call list_lights")

    if color_temp_kelvin is not None and rgb_color is not None:
        return _fail("pass either color_temp_kelvin or rgb_color, not both")

    # Capability gates: a clear refusal beats a silent no-op.
    if brightness_pct is not None:
        if not match["dimmable"]:
            return _fail(f"'{match['name']}' is not dimmable",
                         supported_color_modes=match["supported_color_modes"], permanent=True)
        if not 0 <= int(brightness_pct) <= 100:
            return _fail("brightness_pct must be 0-100")
    if color_temp_kelvin is not None and not match["supports_color_temp"]:
        return _fail(f"'{match['name']}' does not support color temperature",
                     supported_color_modes=match["supported_color_modes"], permanent=True)
    if rgb_color is not None:
        if not match["supports_rgb"]:
            return _fail(f"'{match['name']}' does not support RGB color",
                         supported_color_modes=match["supported_color_modes"], permanent=True)
        if len(rgb_color) != 3 or not all(0 <= int(v) <= 255 for v in rgb_color):
            return _fail("rgb_color must be [r,g,b] with each value 0-255")

    payload: dict[str, Any] = {"entity_id": entity_id}
    if action in ("on", "toggle"):
        if brightness_pct is not None:
            payload["brightness_pct"] = int(brightness_pct)
        if color_temp_kelvin is not None:
            lo, hi = match["min_kelvin"], match["max_kelvin"]
            k = int(color_temp_kelvin)
            if lo and hi:
                k = max(int(lo), min(int(hi), k))
            payload["color_temp_kelvin"] = k
        if rgb_color is not None:
            payload["rgb_color"] = [int(v) for v in rgb_color]
    if transition is not None:
        payload["transition"] = float(transition)

    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action]
    before = {"state": match["state"], "brightness_pct": match["brightness_pct"],
              "color_temp_kelvin": match["color_temp_kelvin"]}

    res = _req("POST", f"/api/services/light/{service}", json=payload)
    if not res["ok"]:
        return res

    settle = max(settle_seconds, (transition or 0) + 0.5)
    time.sleep(max(0.0, settle))

    after_r = _read(entity_id)
    if not after_r["ok"]:
        return {"ok": True, "entity_id": entity_id, "requested": action,
                "confirmed": False, "note": "command accepted but state read-back failed"}
    at = after_r["attributes"]
    after = {"state": after_r["state"], "brightness_pct": _pct_from_255(at.get("brightness")),
             "color_temp_kelvin": at.get("color_temp_kelvin"), "rgb_color": at.get("rgb_color")}

    checks: list[bool] = []
    if action == "off":
        checks.append(after["state"] == "off")
    elif action == "on":
        checks.append(after["state"] == "on")
    else:
        checks.append(after["state"] != before["state"])

    if brightness_pct is not None and action != "off":
        # 0-100 pct maps onto 0-255, so allow small rounding drift.
        checks.append(
            after["brightness_pct"] is not None
            and abs(after["brightness_pct"] - int(brightness_pct)) <= 3
        )
    if color_temp_kelvin is not None and action != "off":
        want = payload.get("color_temp_kelvin")
        got = after["color_temp_kelvin"]
        checks.append(got is not None and abs(int(got) - int(want)) <= 150)

    confirmed = all(checks)
    note = None
    if not confirmed:
        note = (
            "request accepted but observed state does not match. Possible causes: "
            "bulb unreachable/powered off at the switch, value clamped by the "
            "device, or the light is mid-transition. Re-read state or ask the user."
        )
    elif color_temp_kelvin is not None and payload.get("color_temp_kelvin") != int(color_temp_kelvin):
        note = (
            f"requested {int(color_temp_kelvin)}K was clamped to "
            f"{payload['color_temp_kelvin']}K by this light's supported range "
            f"({match['min_kelvin']}-{match['max_kelvin']}K)"
        )

    return {"ok": True, "entity_id": entity_id, "name": match["name"],
            "requested": action, "sent": {k: v for k, v in payload.items() if k != "entity_id"},
            "before": before, "after": after, "confirmed": confirmed, "note": note}


@mcp.tool()
def light_group(
    entity_ids: list[str],
    action: str = "on",
    brightness_pct: int | None = None,
    color_temp_kelvin: int | None = None,
    rgb_color: list[int] | None = None,
    transition: float | None = None,
) -> dict[str, Any]:
    """Command SEVERAL lights, reporting each result separately.

    Use for "turn the kitchen lights down to 20%". Get ids from resolve() or
    list_lights() first. Skips capability-incompatible lights with a clear reason
    rather than failing the whole batch.

    Args:
        entity_ids: exact ids. Never invent them.
        action: "on" | "off" | "toggle"
        brightness_pct: 0-100.
        color_temp_kelvin: Kelvin; clamped per-light.
        rgb_color: [r,g,b] 0-255.
        transition: fade seconds.
    """
    if not entity_ids:
        return _fail("entity_ids is empty - call resolve() or list_lights() first")
    results = [
        light_command(entity_id=e, action=action, brightness_pct=brightness_pct,
                      color_temp_kelvin=color_temp_kelvin, rgb_color=rgb_color,
                      transition=transition)
        for e in entity_ids
    ]
    ok = [r for r in results if r.get("confirmed")]
    skipped = [r for r in results if r.get("permanent")]
    return {"ok": True, "requested": action, "total": len(results),
            "confirmed_count": len(ok), "all_confirmed": len(ok) == len(results),
            "unsupported_count": len(skipped), "results": results}


# ------------------------------------------------------------------ scenes


@mcp.tool()
def list_scenes(area: str = "") -> dict[str, Any]:
    """List available scenes (including Hue scenes surfaced through HA).

    Call before activate_scene so the exact entity_id is used. Scene names are
    user-defined, so fuzzy-matching them via resolve() is often easier.

    Args:
        area: optional case-insensitive name/area filter.
    """
    items, err = _entities("scene")
    if err:
        return err
    items = _filter_area(items, area)
    return {"ok": True, "count": len(items), "scenes": items}


@mcp.tool()
def activate_scene(entity_id: str, transition: float | None = None) -> dict[str, Any]:
    """Activate one scene.

    Scenes have no readable "active" state - HA only records when the scene was
    last triggered. So `confirmed` here means "HA accepted and timestamped the
    activation", which is weaker than for covers/lights. Say so if it matters;
    verify the individual lights with list_lights if the user needs certainty.

    Args:
        entity_id: exact scene id from list_scenes/resolve.
        transition: optional fade seconds.
    """
    if not entity_id.startswith("scene."):
        return _fail(f"'{entity_id}' is not a scene entity - call list_scenes()")
    before = _read(entity_id)
    before_ts = before.get("state") if before["ok"] else None

    payload: dict[str, Any] = {"entity_id": entity_id}
    if transition is not None:
        payload["transition"] = float(transition)
    res = _req("POST", "/api/services/scene/turn_on", json=payload)
    if not res["ok"]:
        return res

    time.sleep(max(1.0, (transition or 0) + 0.5))
    after = _read(entity_id)
    after_ts = after.get("state") if after["ok"] else None

    return {
        "ok": True, "entity_id": entity_id,
        "activated_at": after_ts,
        "confirmed": bool(after_ts) and after_ts != before_ts,
        "note": (
            "scene activation is timestamp-based, not a readable on/off state. "
            "Verify individual lights with list_lights if certainty is required."
        ),
    }


# ------------------------------------------------------------------ areas


def _match_area(areas: list[dict[str, Any]], query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fuzzy-match a spoken room name against list_areas() output.

    Mirrors resolve(): an exact name/alias match wins outright; anything else
    that is ambiguous comes back as candidates rather than a guess.
    """
    q = query.strip().lower()
    exact = [a for a in areas if a["name"].strip().lower() == q or q in [al.lower() for al in a["aliases"]]]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    partial = [a for a in areas if q in a["name"].lower() or any(q in al.lower() for al in a["aliases"])]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


@mcp.tool()
def list_areas() -> dict[str, Any]:
    """List every area/room Home Assistant knows about: id, name, aliases.

    Discovery tool - call before assign_area() or create_area() so a real
    area_id is used, never invented.
    """
    res = _ws_call("config/area_registry/list")
    if not res["ok"]:
        return res
    areas = [
        {"area_id": a.get("area_id"), "name": a.get("name"),
         "aliases": list(a.get("aliases") or []), "floor_id": a.get("floor_id")}
        for a in (res["data"] or [])
    ]
    return {"ok": True, "count": len(areas), "areas": areas}


@mcp.tool()
def create_area(name: str, aliases: list[str] | None = None) -> dict[str, Any]:
    """Create a new area/room in Home Assistant.

    Refuses if an area with this name, or a name/alias that already contains
    or is contained by it, exists - call list_areas() first and use
    assign_area() if the room is already there. A near-duplicate ("Office"
    next to "Office 2") is a room split across two names that look identical
    in conversation, and is worse than a refusal that forces a check.

    Args:
        name: the room name, e.g. "Home Office".
        aliases: other names this room is called, e.g. ["study", "back room"].
    """
    if not name.strip():
        return _fail("name is empty")
    listed = list_areas()
    if not listed["ok"]:
        return listed
    match, candidates = _match_area(listed["areas"], name)
    existing = match or (candidates[0] if candidates else None)
    if existing:
        return _fail(
            f"an area named '{existing['name']}' already exists (area_id={existing['area_id']})",
            area_id=existing.get("area_id"), permanent=True,
        )
    res = _ws_call("config/area_registry/create", name=name, aliases=list(aliases or []))
    if not res["ok"]:
        return res
    d = res["data"] or {}
    return {"ok": True, "area_id": d.get("area_id"), "name": d.get("name"),
            "aliases": list(d.get("aliases") or [])}


@mcp.tool()
def assign_area(entity_id: str, area: str) -> dict[str, Any]:
    """Move one entity into an area/room.

    `area` is matched the same way resolve() matches devices - name or alias,
    case-insensitive. Ambiguous or unknown names are refused with the
    candidate list, never guessed.

    This sets the ENTITY-level area, which overrides whatever area the
    entity's device has - correct for "the theater lamp is in the theater"
    regardless of what device it is paired with. Call list_areas() first if
    unsure what exists.

    Args:
        entity_id: exact id, e.g. "light.theater_lamp".
        area: room name or alias, e.g. "theater".
    """
    state = _read(entity_id)
    if not state["ok"]:
        return state
    listed = list_areas()
    if not listed["ok"]:
        return listed
    match, candidates = _match_area(listed["areas"], area)
    if not match:
        return _fail(
            f"'{area}' matches more than one area" if candidates else f"no area matching '{area}'",
            known_areas=[a["name"] for a in listed["areas"]],
            candidates=[a["name"] for a in candidates] or None,
        )
    res = _ws_call("config/entity_registry/update", entity_id=entity_id, area_id=match["area_id"])
    if not res["ok"]:
        return res
    check = _ws_call("config/entity_registry/get", entity_id=entity_id)
    confirmed = check["ok"] and (check["data"] or {}).get("area_id") == match["area_id"]
    return {
        "ok": True, "entity_id": entity_id,
        "area_id": match["area_id"], "area_name": match["name"],
        "confirmed": confirmed,
        "note": None if confirmed else (
            "area write accepted but read-back did not show the change - re-check with list_areas"
        ),
    }


# ------------------------------------------------------------------ automations
#
# These author NATIVE Home Assistant automations - HA's own engine triggers
# them, this agent is never in the runtime path. See ../AUTOMATIONS_DESIGN.md.


class _AutomationBuildError(Exception):
    """Raised while translating our typed trigger/condition/action dicts into
    Home Assistant's schema. Always caught inside the tool function and
    turned into the same _fail() shape every other tool uses."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


ALLOWED_TRIGGER_KINDS = {"state", "sun", "time", "numeric_state"}
ALLOWED_CONDITION_KINDS = {"state", "numeric_state", "sun", "time", "and", "or"}
# Deliberately NOT "notify", "script", or a raw service escape hatch - only
# the domains this server already exposes for live control. See
# AUTOMATIONS_DESIGN.md §3: an automation is standing infrastructure, and a
# bad action step here keeps firing long after the conversation that wrote
# it is gone. Never add lock/alarm/garage/script kinds without the same
# deliberate tiering decision FUTURE.md §1 made for live control.
ALLOWED_ACTION_KINDS = {"light", "cover", "scene", "delay"}


def _offset_hhmmss(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    total = abs(int(minutes))
    h, m = divmod(total, 60)
    return f"{sign}{h:02d}:{m:02d}:00"


def _build_trigger(t: dict[str, Any]) -> dict[str, Any]:
    kind = str(t.get("kind") or "").strip().lower()
    if kind not in ALLOWED_TRIGGER_KINDS:
        raise _AutomationBuildError(
            f"unknown trigger kind '{kind}' - use one of {sorted(ALLOWED_TRIGGER_KINDS)}")
    if kind in ("state", "numeric_state"):
        eid = t.get("entity_id")
        if not eid:
            raise _AutomationBuildError(f"{kind} trigger needs entity_id")
        r = _read(eid)
        if not r["ok"]:
            raise _AutomationBuildError(
                f"trigger entity '{eid}' does not exist - call get_state() or resolve() first")
        out: dict[str, Any] = {"trigger": kind, "entity_id": eid}
        if t.get("for_seconds") is not None:
            out["for"] = {"seconds": int(t["for_seconds"])}
        if kind == "state":
            if t.get("to") is not None:
                out["to"] = t["to"]
            if t.get("from_") is not None:
                out["from"] = t["from_"]
        else:
            if t.get("above") is not None:
                out["above"] = t["above"]
            if t.get("below") is not None:
                out["below"] = t["below"]
            if "above" not in out and "below" not in out:
                raise _AutomationBuildError("numeric_state trigger needs 'above' and/or 'below'")
        return out
    if kind == "sun":
        event = str(t.get("event") or "").strip().lower()
        if event not in ("sunrise", "sunset"):
            raise _AutomationBuildError("sun trigger 'event' must be 'sunrise' or 'sunset'")
        out = {"trigger": "sun", "event": event}
        offset = t.get("offset_minutes")
        if offset:
            out["offset"] = _offset_hhmmss(int(offset))
        return out
    if kind == "time":
        at = t.get("at")
        if not at:
            raise _AutomationBuildError("time trigger needs 'at' (\"HH:MM:SS\")")
        return {"trigger": "time", "at": at}
    raise _AutomationBuildError(f"unhandled trigger kind '{kind}'")  # pragma: no cover


def _build_condition(c: dict[str, Any]) -> dict[str, Any]:
    kind = str(c.get("kind") or "").strip().lower()
    if kind not in ALLOWED_CONDITION_KINDS:
        raise _AutomationBuildError(
            f"unknown condition kind '{kind}' - use one of {sorted(ALLOWED_CONDITION_KINDS)}")
    if kind in ("and", "or"):
        sub = c.get("conditions") or []
        if not sub:
            raise _AutomationBuildError(f"'{kind}' condition needs a non-empty conditions list")
        return {"condition": kind, "conditions": [_build_condition(s) for s in sub]}
    if kind in ("state", "numeric_state"):
        eid = c.get("entity_id")
        if not eid:
            raise _AutomationBuildError(f"{kind} condition needs entity_id")
        r = _read(eid)
        if not r["ok"]:
            raise _AutomationBuildError(f"condition entity '{eid}' does not exist")
        out: dict[str, Any] = {"condition": kind, "entity_id": eid}
        if kind == "state":
            if c.get("state") is None:
                raise _AutomationBuildError("state condition needs 'state'")
            out["state"] = c["state"]
        else:
            if c.get("above") is not None:
                out["above"] = c["above"]
            if c.get("below") is not None:
                out["below"] = c["below"]
            if "above" not in out and "below" not in out:
                raise _AutomationBuildError("numeric_state condition needs 'above' and/or 'below'")
        return out
    if kind in ("sun", "time"):
        out = {"condition": kind}
        if c.get("after"):
            out["after"] = c["after"]
        if c.get("before"):
            out["before"] = c["before"]
        if "after" not in out and "before" not in out:
            raise _AutomationBuildError(f"{kind} condition needs 'after' and/or 'before'")
        return out
    raise _AutomationBuildError(f"unhandled condition kind '{kind}'")  # pragma: no cover


def _build_action(a: dict[str, Any]) -> dict[str, Any]:
    kind = str(a.get("kind") or "").strip().lower()
    if kind not in ALLOWED_ACTION_KINDS:
        raise _AutomationBuildError(
            f"unknown action kind '{kind}' - use one of {sorted(ALLOWED_ACTION_KINDS)}. "
            "Locks, alarm, garage, scripts, notify, and raw service calls are refused "
            "here on purpose - this server does not expose them for live control either."
        )
    if kind == "delay":
        seconds = a.get("seconds")
        if seconds is None or float(seconds) <= 0:
            raise _AutomationBuildError("delay action needs a positive 'seconds'")
        return {"delay": {"seconds": float(seconds)}}
    if kind == "scene":
        eid = a.get("entity_id")
        if not eid or not str(eid).startswith("scene."):
            raise _AutomationBuildError(f"'{eid}' is not a scene entity - call list_scenes()")
        scenes, err = _entities("scene")
        if err:
            raise _AutomationBuildError(err.get("error") or "could not verify scene")
        if not any(s["entity_id"] == eid for s in scenes):
            raise _AutomationBuildError(f"scene '{eid}' does not exist - call list_scenes()")
        return {"action": "scene.turn_on", "target": {"entity_id": eid}}
    if kind == "light":
        eid = a.get("entity_id")
        if not eid or not str(eid).startswith("light."):
            raise _AutomationBuildError(f"'{eid}' is not a light entity - call list_lights()")
        act = str(a.get("action") or "on").strip().lower()
        if act not in ("on", "off", "toggle"):
            raise _AutomationBuildError(f"unknown light action '{act}' - use on/off/toggle")
        lights, err = _entities("light")
        if err:
            raise _AutomationBuildError(err.get("error") or "could not verify light")
        match = next((l for l in lights if l["entity_id"] == eid), None)
        if match is None:
            raise _AutomationBuildError(f"light '{eid}' does not exist - call list_lights()")
        data: dict[str, Any] = {}
        if a.get("brightness_pct") is not None:
            if not match["dimmable"]:
                raise _AutomationBuildError(
                    f"'{match['name']}' is not dimmable", supported_color_modes=match["supported_color_modes"])
            data["brightness_pct"] = int(a["brightness_pct"])
        if a.get("color_temp_kelvin") is not None:
            if not match["supports_color_temp"]:
                raise _AutomationBuildError(f"'{match['name']}' does not support color temperature")
            data["color_temp_kelvin"] = int(a["color_temp_kelvin"])
        if a.get("rgb_color") is not None:
            if not match["supports_rgb"]:
                raise _AutomationBuildError(f"'{match['name']}' does not support RGB color")
            data["rgb_color"] = [int(v) for v in a["rgb_color"]]
        if a.get("transition") is not None:
            data["transition"] = float(a["transition"])
        service = {"on": "light.turn_on", "off": "light.turn_off", "toggle": "light.toggle"}[act]
        step: dict[str, Any] = {"action": service, "target": {"entity_id": eid}}
        if data:
            step["data"] = data
        return step
    if kind == "cover":
        eid = a.get("entity_id")
        if not eid or not str(eid).startswith("cover."):
            raise _AutomationBuildError(f"'{eid}' is not a cover entity - call list_covers()")
        act = str(a.get("action") or "").strip().lower()
        services = {"open": "cover.open_cover", "close": "cover.close_cover", "position": "cover.set_cover_position"}
        if act not in services:
            raise _AutomationBuildError(f"unknown cover action '{act}' - use open/close/position")
        covers, err = _entities("cover")
        if err:
            raise _AutomationBuildError(err.get("error") or "could not verify cover")
        match = next((c for c in covers if c["entity_id"] == eid), None)
        if match is None:
            raise _AutomationBuildError(f"cover '{eid}' does not exist - call list_covers()")
        needed = {"position": "set_position"}.get(act, act)
        if needed not in match["supports"]:
            raise _AutomationBuildError(f"'{match['name']}' does not support '{needed}'", supports=match["supports"])
        step = {"action": services[act], "target": {"entity_id": eid}}
        if act == "position":
            position = a.get("position")
            if position is None or not 0 <= int(position) <= 100:
                raise _AutomationBuildError("position must be 0-100 (0=closed, 100=open)")
            step["data"] = {"position": int(position)}
        return step
    raise _AutomationBuildError(f"unhandled action kind '{kind}'")  # pragma: no cover


def _find_automation_entity(automation_id: str) -> dict[str, Any] | None:
    """Find the automation.* state whose attributes.id matches - never guess
    the entity_id from a slugified alias, HA's own slugification and
    collision suffixing is not this server's to predict."""
    res = _req("GET", "/api/states")
    if not res["ok"]:
        return None
    for s in res["data"]:
        eid = s.get("entity_id", "")
        if eid.startswith("automation.") and (s.get("attributes") or {}).get("id") == automation_id:
            return s
    return None


def _label_as_managed(entity_id: str) -> dict[str, Any]:
    got = _ws_call("config/entity_registry/get", entity_id=entity_id)
    if not got["ok"]:
        return got
    existing = list((got["data"] or {}).get("labels") or [])
    if "hermes-managed" in existing:
        return {"ok": True}
    return _ws_call("config/entity_registry/update", entity_id=entity_id,
                     labels=existing + ["hermes-managed"])


@mcp.tool()
def list_automations(managed_only: bool = True) -> dict[str, Any]:
    """List automations that exist in Home Assistant right now.

    Defaults to ones this server created (labeled `hermes-managed`) - set
    managed_only=False to see everything, including hand-written automations
    this server does not understand and should not casually modify.

    Args:
        managed_only: True (default) = only automations this server manages.
    """
    states = _req("GET", "/api/states")
    if not states["ok"]:
        return states
    reg = _ws_call("config/entity_registry/list")
    labels_by_entity: dict[str, set[str]] = {}
    if reg["ok"]:
        labels_by_entity = {e.get("entity_id"): set(e.get("labels") or []) for e in (reg["data"] or [])}

    out = []
    for s in states["data"]:
        eid = s.get("entity_id", "")
        if not eid.startswith("automation."):
            continue
        attrs = s.get("attributes") or {}
        managed = "hermes-managed" in labels_by_entity.get(eid, set())
        if managed_only and not managed:
            continue
        out.append({
            "automation_id": attrs.get("id"), "entity_id": eid,
            "name": attrs.get("friendly_name"), "state": s.get("state"),
            "last_triggered": attrs.get("last_triggered"), "managed": managed,
        })
    return {"ok": True, "count": len(out), "automations": out}


@mcp.tool()
def get_automation(automation_id: str) -> dict[str, Any]:
    """Read one automation's current config: alias, triggers, conditions,
    actions, mode, and live entity_id/state.

    Args:
        automation_id: the id from create_automation() or list_automations().
    """
    res = _req("GET", f"/api/config/automation/config/{automation_id}")
    if not res["ok"]:
        return res
    cfg = res["data"] if isinstance(res["data"], dict) else {}
    entity = _find_automation_entity(automation_id)
    return {
        "ok": True, "automation_id": automation_id,
        "alias": cfg.get("alias"), "description": cfg.get("description"),
        "mode": cfg.get("mode", "single"),
        "triggers": cfg.get("triggers", cfg.get("trigger", [])),
        "conditions": cfg.get("conditions", cfg.get("condition", [])),
        "actions": cfg.get("actions", cfg.get("action", [])),
        "entity_id": entity.get("entity_id") if entity else None,
        "state": entity.get("state") if entity else None,
    }


@mcp.tool()
def create_automation(
    alias: str,
    triggers: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    conditions: list[dict[str, Any]] | None = None,
    mode: str = "single",
    enabled: bool = True,
    automation_id: str | None = None,
) -> dict[str, Any]:
    """Author a NATIVE Home Assistant automation - HA's own engine does the
    triggering, this agent is never in the runtime path. Call this again with
    the same alias (or the same explicit automation_id) to UPDATE it instead
    of creating a duplicate - check list_automations() first either way.

    Every entity_id must come from resolve()/list_* - never invented. Action
    steps get the SAME capability checks as light_command/cover_command (a
    non-dimmable light with brightness_pct set is refused here too, before
    anything is saved).

    Trigger kinds (need >=1):
      state:         entity_id, to, from_, for_seconds
      numeric_state:  entity_id, above, below, for_seconds
      sun:           event ("sunrise"|"sunset"), offset_minutes (+/-)
      time:          at ("HH:MM:SS")
    Condition kinds (ANDed together; optional):
      state, numeric_state (as above), sun/time (after/before: "sunrise"/
      "sunset"/"HH:MM:SS"), and/or (nested "conditions" list)
    Action kinds (need >=1) - ONLY these, ever, no exceptions:
      light: entity_id, action ("on"|"off"|"toggle"), brightness_pct,
             color_temp_kelvin, rgb_color, transition
      cover: entity_id, action ("open"|"close"|"position"), position
      scene: entity_id
      delay: seconds

    Args:
        alias: human-readable name, e.g. "Hallway motion -> light".
        triggers: list of Trigger dicts, see above.
        actions: list of Action dicts, see above.
        conditions: optional list of Condition dicts, ANDed together.
        mode: HA automation mode - "single" (default), "restart", "queued", "parallel".
        enabled: create it enabled (default) or disabled for later review.
        automation_id: omit to derive one from `alias`; pass an existing id
            to be explicit about which automation is being updated.
    """
    if not alias.strip():
        return _fail("alias is empty")
    if not triggers:
        return _fail("triggers is empty - an automation needs at least one trigger")
    if not actions:
        return _fail("actions is empty - an automation needs at least one action")

    aid = automation_id or f"hermes_{_slugify(alias)}"

    try:
        built_triggers = [_build_trigger(t) for t in triggers]
        built_conditions = [_build_condition(c) for c in (conditions or [])]
        built_actions = [_build_action(a) for a in actions]
    except _AutomationBuildError as e:
        return _fail(str(e), **e.extra)

    pre = _req("GET", f"/api/config/automation/config/{aid}")
    existed = bool(pre["ok"])

    payload = {
        "id": aid,
        "alias": alias,
        "description": "Hermes-managed. Edit via ha-mcp, not the UI, or the two will drift.",
        "triggers": built_triggers,
        "conditions": built_conditions,
        "actions": built_actions,
        "mode": mode,
    }
    res = _req("POST", f"/api/config/automation/config/{aid}", json=payload)
    if not res["ok"]:
        return res

    entity = _find_automation_entity(aid)
    if entity is None:
        return {
            "ok": True, "automation_id": aid, "created": not existed, "confirmed": False,
            "note": (
                "config saved but no matching automation.* entity was found yet - "
                "Home Assistant may need a moment to reload; re-check with get_automation()."
            ),
        }

    if enabled != (entity.get("state") == "on"):
        svc = "turn_on" if enabled else "turn_off"
        toggled = _req("POST", f"/api/services/automation/{svc}", json={"entity_id": entity["entity_id"]})
        if toggled["ok"]:
            time.sleep(0.5)
            entity = _find_automation_entity(aid) or entity

    label_res = _label_as_managed(entity["entity_id"])
    warning = None if label_res["ok"] else (
        f"automation saved but could not be labeled hermes-managed: {label_res.get('error')}"
    )

    confirmed = (entity.get("state") == "on") == enabled
    return {
        "ok": True, "automation_id": aid, "entity_id": entity["entity_id"],
        "alias": alias, "created": not existed, "state": entity.get("state"),
        "confirmed": confirmed, "warning": warning,
    }


@mcp.tool()
def automation_command(automation_id: str, action: str) -> dict[str, Any]:
    """Enable, disable, manually trigger, or delete one automation.

    `trigger` runs the automation's actions right now, ignoring its own
    triggers and conditions - use it to test "sunrise -> open blinds" without
    waiting for sunrise. `delete` returns the full config that was removed so
    it can be recreated with create_automation() if this was a mistake - Home
    Assistant has no undo for this.

    Args:
        automation_id: the id from create_automation() or list_automations().
        action: "enable" | "disable" | "trigger" | "delete"
    """
    action = action.strip().lower()
    entity = _find_automation_entity(automation_id)
    if entity is None:
        return _fail(f"no automation with id '{automation_id}' - call list_automations()")
    eid = entity["entity_id"]

    if action == "delete":
        deleted_cfg = get_automation(automation_id)
        res = _req("DELETE", f"/api/config/automation/config/{automation_id}")
        if not res["ok"]:
            return res
        return {"ok": True, "automation_id": automation_id, "entity_id": eid, "deleted": True,
                "deleted_config": deleted_cfg if deleted_cfg.get("ok") else None}

    services = {"enable": "turn_on", "disable": "turn_off", "trigger": "trigger"}
    if action not in services:
        return _fail(f"unknown action '{action}' - use enable/disable/trigger/delete")
    res = _req("POST", f"/api/services/automation/{services[action]}", json={"entity_id": eid})
    if not res["ok"]:
        return res
    time.sleep(0.5)
    after = _find_automation_entity(automation_id) or entity
    if action == "enable":
        confirmed = after.get("state") == "on"
    elif action == "disable":
        confirmed = after.get("state") == "off"
    else:
        confirmed = True  # "trigger" has no readable "it ran" signal beyond last_triggered
    return {
        "ok": True, "automation_id": automation_id, "entity_id": eid, "requested": action,
        "state": after.get("state"),
        "last_triggered": (after.get("attributes") or {}).get("last_triggered"),
        "confirmed": confirmed,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
