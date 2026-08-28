"""Run this against a REAL Home Assistant instance — not a fake.

Exercises the two wire formats ha-mcp depends on (REST for automations,
WebSocket for areas/entity registry) directly, independent of ha_mcp/server.py,
so a bug in that module can't hide a wire-format problem or vice versa.

Every write this script makes is either physically inert (a `delay` action,
never a device) or cleaned up before exit, including on failure - it is safe
to run against a real house.

Usage:
    set HASS_URL=http://192.168.1.x:8123
    set HASS_TOKEN=your-long-lived-token
    C:\\dev\\hermes-tools\\ha-mcp\\.venv\\Scripts\\python.exe verify_live.py

(or the bash equivalent - export HASS_URL / HASS_TOKEN first)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import websockets.sync.client as wsc
except ImportError:
    print("FAIL: the 'websockets' package is not installed in this Python.")
    print("Run this with ha-mcp's own venv: .venv\\Scripts\\python.exe verify_live.py")
    sys.exit(1)

BASE_URL = (os.environ.get("HASS_URL") or "").rstrip("/")
TOKEN = os.environ.get("HASS_TOKEN") or ""

if not BASE_URL or not TOKEN:
    print("Set HASS_URL and HASS_TOKEN before running this.")
    sys.exit(1)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    (PASS if ok else FAIL).append(name)
    print(f"[{tag}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def req(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def ws_call(msg_type: str, **fields) -> dict:
    ws_url = BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    with wsc.connect(ws_url, open_timeout=15, close_timeout=15) as ws:
        hello = json.loads(ws.recv(timeout=15))
        if hello.get("type") != "auth_required":
            return {"success": False, "error": {"message": f"unexpected handshake: {hello}"}}
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        auth = json.loads(ws.recv(timeout=15))
        if auth.get("type") != "auth_ok":
            return {"success": False, "error": {"message": "auth failed - bad token"}}
        ws.send(json.dumps({"id": 1, "type": msg_type, **fields}))
        return json.loads(ws.recv(timeout=15))


print(f"Target: {BASE_URL}\n")

# ---------------------------------------------------------------- 1. REST auth
status, data = req("GET", "/api/config")
check("REST auth + reachability (/api/config)", status == 200,
      f"HTTP {status}" if status != 200 else f"HA {data.get('version') if isinstance(data, dict) else '?'}")

# ------------------------------------------------------- 2. automation lifecycle
AID = "ha_mcp_verify_live_script"
ALIAS = "ha-mcp verify_live.py smoke test"

status, _ = req("DELETE", f"/api/config/automation/config/{AID}")  # clean slate

status, data = req("POST", f"/api/config/automation/config/{AID}", {
    "id": AID, "alias": ALIAS,
    "triggers": [{"trigger": "time", "at": "23:59:59"}],
    "conditions": [],
    "actions": [{"delay": {"seconds": 1}}],  # inert - touches no device
    "mode": "single",
})
check("create automation (POST /api/config/automation/config/<id>)",
      status == 200 and isinstance(data, dict) and data.get("result") == "ok",
      f"HTTP {status}: {data}")

status, data = req("GET", f"/api/config/automation/config/{AID}")
schema_ok = (
    status == 200 and isinstance(data, dict)
    and data.get("alias") == ALIAS
    and data.get("triggers") == [{"trigger": "time", "at": "23:59:59"}]
    and data.get("actions") == [{"delay": {"seconds": 1}}]
)
check("read it back with the exact schema sent", schema_ok, f"HTTP {status}: {data}")

# Find the live entity_id the way ha_mcp/server.py does: by attributes.id,
# never by guessing a slug from the alias.
entity_id = None
for _ in range(6):
    status, states = req("GET", "/api/states")
    if status == 200:
        for s in states:
            if s.get("entity_id", "").startswith("automation.") and (s.get("attributes") or {}).get("id") == AID:
                entity_id = s["entity_id"]
                break
    if entity_id:
        break
    time.sleep(1)
check("automation.* entity appears with matching attributes.id", entity_id is not None,
      entity_id or "not found after 6s - HA may not have reloaded automations yet")

if entity_id:
    status, data = req("POST", "/api/services/automation/trigger", {"entity_id": entity_id})
    check("automation/trigger service call", status == 200, f"HTTP {status}: {data}")

status, data = req("DELETE", f"/api/config/automation/config/{AID}")
check("delete automation (cleanup)", status == 200, f"HTTP {status}: {data}")

# --------------------------------------------------------------- 3. WebSocket
reply = ws_call("config/area_registry/list")
areas_ok = reply.get("success") is True and isinstance(reply.get("result"), list)
check("WebSocket auth + config/area_registry/list", areas_ok, str(reply)[:200] if not areas_ok else f"{len(reply['result'])} area(s)")

AREA_NAME = "ha-mcp verify_live.py smoke test area"
create_reply = ws_call("config/area_registry/create", name=AREA_NAME, aliases=[])
area_id = (create_reply.get("result") or {}).get("area_id") if create_reply.get("success") else None
check("WebSocket area create", area_id is not None, str(create_reply)[:200] if not area_id else area_id)

if area_id:
    get_reply = ws_call("config/area_registry/list")
    found = any(a.get("area_id") == area_id and a.get("name") == AREA_NAME for a in (get_reply.get("result") or []))
    check("created area shows up in a fresh list", found)

    del_reply = ws_call("config/area_registry/delete", area_id=area_id)
    check("WebSocket area delete (cleanup)", del_reply.get("success") is True, str(del_reply)[:200])

# --------------------------------------------------------------------- summary
print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")
if FAIL:
    print("Failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
