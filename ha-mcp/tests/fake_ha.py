"""A Home Assistant that isn't there, for both transports this server uses.

ha_mcp.server has exactly two choke points - _req (REST) and _ws_call
(WebSocket) - so faking Home Assistant is faking those two functions and
nothing else. Real-transport code (httpx.Client, websockets.sync.client)
never runs in these tests.

The interesting behaviour to catch here is quiet failure: a POST that returns
200 while nothing actually happened, an automation whose entity_id cannot be
guessed from its alias, a capability check that should refuse before a write
rather than after. FakeHomeAssistant is deliberately literal about state so
those cases are easy to set up.
"""

from __future__ import annotations

import re
from typing import Any


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_") or "x"


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.automations: dict[str, dict[str, Any]] = {}
        self.areas: dict[str, dict[str, Any]] = {}
        self.entity_registry: dict[str, dict[str, Any]] = {}
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        # A service that changes nothing - the bulb off at the wall.
        self.deaf: set[str] = set()
        self._next_area = 1

    # ---------------------------------------------------------------- setup

    def entity(self, entity_id: str, state: str, **attrs: Any) -> "FakeHomeAssistant":
        self.states[entity_id] = {"entity_id": entity_id, "state": state, "attributes": attrs}
        self.entity_registry.setdefault(entity_id, {"entity_id": entity_id, "area_id": None, "labels": []})
        return self

    def area(self, name: str, aliases: list[str] | None = None) -> str:
        area_id = f"area_{self._next_area}"
        self._next_area += 1
        self.areas[area_id] = {"area_id": area_id, "name": name,
                                "aliases": list(aliases or []), "floor_id": None}
        return area_id

    def install(self, module) -> "FakeHomeAssistant":
        module._req = self.req
        module._ws_call = self.ws
        return self

    # ----------------------------------------------------------------- REST

    def req(self, method: str, path: str, json: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        path = path.split("/api/", 1)[-1]  # server always calls with a leading "/api/..."

        if path == "template":
            return {"ok": True, "data": ""}

        if path == "states" and method == "GET":
            return {"ok": True, "data": list(self.states.values())}

        if path.startswith("states/"):
            eid = path.split("/", 1)[1]
            if eid not in self.states:
                return {"ok": False, "error": f"404 not found: /api/{path} (entity or endpoint does not exist)"}
            return {"ok": True, "data": self.states[eid]}

        if path.startswith("services/"):
            _, domain, service = path.split("/", 2)
            data = json or {}
            self.service_calls.append((domain, service, data))
            self._apply(domain, service, data)
            return {"ok": True, "data": {}}

        if path.startswith("config/automation/config/"):
            aid = path.split("/", 3)[3]
            if method == "GET":
                if aid not in self.automations:
                    return {"ok": False, "error": f"404 not found: /api/{path}"}
                return {"ok": True, "data": self.automations[aid]}
            if method == "POST":
                cfg = dict(json or {})
                self.automations[aid] = cfg
                existing_eid = next(
                    (e for e, row in self.states.items() if row["attributes"].get("id") == aid), None)
                eid = existing_eid or f"automation.{_slug(cfg.get('alias', aid))}"
                prior_state = self.states.get(eid, {}).get("state", "on")
                self.states[eid] = {
                    "entity_id": eid, "state": prior_state,
                    "attributes": {
                        "id": aid, "friendly_name": cfg.get("alias"),
                        "last_triggered": self.states.get(eid, {}).get("attributes", {}).get("last_triggered"),
                    },
                }
                self.entity_registry.setdefault(eid, {"entity_id": eid, "area_id": None, "labels": []})
                return {"ok": True, "data": {"result": "ok"}}
            if method == "DELETE":
                if aid not in self.automations:
                    return {"ok": False, "error": f"404 not found: /api/{path}"}
                del self.automations[aid]
                eid = next((e for e, row in self.states.items() if row["attributes"].get("id") == aid), None)
                if eid:
                    del self.states[eid]
                    self.entity_registry.pop(eid, None)
                return {"ok": True, "data": {"result": "ok"}}

        raise AssertionError(f"FakeHomeAssistant.req got an unexpected {method} {path!r}")

    def _apply(self, domain: str, service: str, data: dict[str, Any]) -> None:
        eid = data.get("entity_id")
        if isinstance(eid, list):
            eid = eid[0] if eid else None
        if eid is None or eid not in self.states:
            return
        row = self.states[eid]
        if domain == "automation":
            if service == "turn_on":
                row["state"] = "on"
            elif service == "turn_off":
                row["state"] = "off"
            elif service == "trigger":
                row["attributes"]["last_triggered"] = "2026-08-28T00:00:00+00:00"
            return
        if eid in self.deaf:
            return
        if domain == "light":
            row["state"] = "on" if service in ("turn_on", "toggle") else "off"
            if "brightness_pct" in data:
                row["attributes"]["brightness"] = round(data["brightness_pct"] * 2.55)
            if "color_temp_kelvin" in data:
                row["attributes"]["color_temp_kelvin"] = data["color_temp_kelvin"]
        elif domain == "cover":
            if service == "set_cover_position":
                row["attributes"]["current_position"] = data["position"]
                row["state"] = "open" if data["position"] else "closed"
            elif service == "open_cover":
                row["state"] = "open"
            elif service == "close_cover":
                row["state"] = "closed"

    # ------------------------------------------------------------ WebSocket

    def ws(self, msg_type: str, **fields: Any) -> dict[str, Any]:
        if msg_type == "config/area_registry/list":
            return {"ok": True, "data": list(self.areas.values())}

        if msg_type == "config/area_registry/create":
            name = fields["name"]
            if any(a["name"].lower() == name.lower() for a in self.areas.values()):
                return {"ok": False, "error": f"area '{name}' already exists"}
            area_id = self.area(name, fields.get("aliases"))
            return {"ok": True, "data": self.areas[area_id]}

        if msg_type == "config/entity_registry/list":
            return {"ok": True, "data": list(self.entity_registry.values())}

        if msg_type == "config/entity_registry/get":
            row = self.entity_registry.get(fields["entity_id"])
            if row is None:
                return {"ok": False, "error": f"no entity_registry entry for {fields['entity_id']!r}"}
            return {"ok": True, "data": row}

        if msg_type == "config/entity_registry/update":
            eid = fields["entity_id"]
            row = self.entity_registry.setdefault(eid, {"entity_id": eid, "area_id": None, "labels": []})
            if "area_id" in fields:
                row["area_id"] = fields["area_id"]
            if "labels" in fields:
                row["labels"] = list(fields["labels"])
            return {"ok": True, "data": row}

        raise AssertionError(f"FakeHomeAssistant.ws got an unexpected command {msg_type!r}")
