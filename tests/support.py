"""Loading a server for tests, and a Home Assistant that isn't there.

The servers are single files that are not importable as packages - they live in
hyphenated directories and read their configuration from the environment at
import time. Both facts are deliberate and neither is worth changing for the
tests, so this module does the loading instead: set the environment, then import
the file by path under a name of our choosing.

The Home Assistant fake matters more than it looks. Every write in hass-mcp
polls the entity back and reports what is actually true, which is the whole
reason that server exists - and it is precisely the behaviour you cannot test
against a real house, because you would need a bulb that is off at the wall on
demand. Here you just say so.
"""

import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load(name, relative_path, env=None):
    """Import a server file fresh, under the given environment."""
    for key, value in (env or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    path = os.path.join(ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registering it means a traceback inside the module reports real lines, and
    # the tool registry it populates in mcpkit is the same object we inspect.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_mcpkit():
    """A private copy of mcpkit, so a test's tool registry is its own."""
    import mcpkit

    fresh = importlib.util.module_from_spec(
        importlib.util.spec_from_file_location("mcpkit_under_test",
                                               os.path.join(ROOT, "mcpkit.py")))
    fresh.__spec__.loader.exec_module(fresh)
    del mcpkit
    return fresh


class FakeHass:
    """Just enough Home Assistant to test the parts that matter.

    Entities are plain dicts. `service_calls` records everything dispatched, so
    a test can assert that the *right* service was called and not merely that
    the state ended up correct - those come apart exactly where the interesting
    bugs are.
    """

    def __init__(self, states=None, version="2025.7.0"):
        self.states = dict(states or {})
        self.service_calls = []
        # A service that changes nothing: the bulb that is off at the wall, the
        # lock with a flat battery. Home Assistant returns 200 for these.
        self.deaf = set()

    def entity(self, entity_id, state, **attributes):
        self.states[entity_id] = {"entity_id": entity_id, "state": state,
                                  "attributes": attributes}
        return self

    def install(self, module):
        """Point a loaded hass-mcp at this fake instead of the network."""
        module.api = self.api
        return self

    def api(self, path, payload=None, method=None):
        path = path.lstrip("/")
        if path == "config":
            return {"version": "2025.7.0", "unit_system": {"temperature": "°C"}}
        if path == "states":
            return list(self.states.values())
        if path.startswith("states/"):
            entity_id = path.split("/", 1)[1]
            if entity_id not in self.states:
                raise self.not_found(entity_id)
            return self.states[entity_id]
        if path.startswith("services/"):
            _, domain, service = path.split("/", 2)
            self.service_calls.append((domain, service, payload))
            self.apply(domain, service, payload or {})
            return {}
        raise AssertionError(f"FakeHass got an unexpected path: {path!r}")

    def not_found(self, entity_id):
        from mcpkit import ToolError
        return ToolError(f"Home Assistant has no entity {entity_id!r} (404).")

    def apply(self, domain, service, data):
        """What a real Home Assistant would eventually do to the state machine."""
        targets = data.get("entity_id") or []
        if isinstance(targets, str):
            targets = [targets]
        for entity_id in targets:
            row = self.states.get(entity_id)
            if row is None or entity_id in self.deaf:
                continue
            attrs = row["attributes"]
            if domain == "light":
                row["state"] = "on" if service == "turn_on" else "off"
                if "brightness_pct" in data:
                    attrs["brightness"] = round(data["brightness_pct"] * 2.55)
                if "color_temp_kelvin" in data and attrs.get("supports_color_temp", True):
                    attrs["color_temp_kelvin"] = data["color_temp_kelvin"]
            elif domain == "cover":
                if service == "set_cover_position":
                    attrs["current_position"] = data["position"]
                    row["state"] = "open" if data["position"] else "closed"
                elif service == "set_cover_tilt_position":
                    attrs["current_tilt_position"] = data["tilt_position"]
                elif service == "open_cover":
                    row["state"] = "open"
                elif service == "close_cover":
                    row["state"] = "closed"
            elif domain == "climate" and service == "set_temperature":
                attrs["temperature"] = data["temperature"]


def write_map(directory, mapping):
    path = os.path.join(directory, "house.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh)
    return path
