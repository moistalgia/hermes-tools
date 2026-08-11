"""
Home Assistant MCP server for Hermes — covers, lights, scenes.

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
"""

from __future__ import annotations

import difflib
import os
import time
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="ha",
    version="2.0.0",
    instructions=(
        "Home Assistant control for covers, lights and scenes. Always call a "
        "list_* or resolve tool first so real entity_ids are used - never invent "
        "them. Cover position is 0=closed/100=open; light brightness is percent "
        "0-100; color temperature is in Kelvin. Never tell the user something "
        "changed unless the result has confirmed=true."
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
