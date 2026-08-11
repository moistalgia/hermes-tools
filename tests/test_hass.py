"""hass-mcp: room resolution, and the read-back that is the point of the server.

Home Assistant returns 200 when a service call is *dispatched*, not when
anything happened. Every write here polls the entity back and reports what is
actually true, and these tests are the only place that behaviour can be checked
- you cannot ask a real house for a bulb that is switched off at the wall on
demand, but FakeHass has `deaf` for exactly that.
"""

import os
import tempfile
import unittest

from support import FakeHass, load, write_map

TMP = tempfile.mkdtemp(prefix="hass-mcp-test-")

HOUSE = {
    "rooms": {
        "office": {
            "aliases": ["study", "back room"],
            "lights": ["light.office_ceiling", "light.office_lamp"],
            "covers": ["cover.office_blind"],
            "climate": ["climate.upstairs"],
        },
        "kitchen": {"lights": ["light.kitchen_main"]},
        "living room": {
            "aliases": ["lounge"],
            "covers": ["cover.living_room_venetian", "cover.living_room_roller"],
        },
        "bedroom": {"covers": ["cover.bedroom_blackout"]},
    },
    "scenes": {"wind down": "scene.wind_down"},
}

hass = load("hass_under_test", "hass-mcp/hass_mcp_server.py",
            env={"HASS_MAP": write_map(TMP, HOUSE), "HASS_TOKEN": "test-token"})

# Cover feature bits, spelled out so the tests read as capabilities.
POSITIONABLE = hass.COVER_OPEN | hass.COVER_CLOSE | hass.COVER_SET_POSITION
TILTABLE = POSITIONABLE | hass.COVER_SET_TILT_POSITION
BINARY_ONLY = hass.COVER_OPEN | hass.COVER_CLOSE


class HassCase(unittest.TestCase):
    def setUp(self):
        hass.POLL_INTERVAL = 0            # no real waiting in tests
        hass.CONFIRM_TIMEOUT = {k: 0 for k in hass.CONFIRM_TIMEOUT}
        hass._map_cache = hass._map_mtime = None
        self.ha = FakeHass()
        (self.ha
         .entity("light.office_ceiling", "off")
         .entity("light.office_lamp", "off")
         .entity("light.kitchen_main", "off")
         .entity("cover.office_blind", "open", current_position=100,
                 supported_features=POSITIONABLE)
         .entity("cover.living_room_venetian", "open", current_position=100,
                 current_tilt_position=100, supported_features=TILTABLE)
         .entity("cover.living_room_roller", "open", supported_features=BINARY_ONLY)
         .entity("cover.bedroom_blackout", "open", current_position=100,
                 supported_features=POSITIONABLE)
         .entity("climate.upstairs", "heat", temperature=19, current_temperature=17.5)
         .entity("scene.wind_down", "2026-03-10T21:00:00"))
        self.ha.install(hass)


class TestRoomResolution(HassCase):
    def test_exact_alias_and_prefix_all_reach_the_room(self):
        for spoken in ("office", "OFFICE", "study", "back room", "offi"):
            key, _cfg = hass.resolve_room(spoken)
            self.assertEqual(key, "office", msg=f"for {spoken!r}")

    def test_an_unknown_room_lists_the_real_ones(self):
        with self.assertRaises(hass.ToolError) as caught:
            hass.resolve_room("garage")
        self.assertIn("kitchen", caught.exception.extra["known_rooms"])

    def test_a_room_without_the_capability_says_so_rather_than_unknown(self):
        # "The kitchen has no blinds" ends the conversation. "Unknown room"
        # sends the agent hunting through synonyms for a room it already found.
        with self.assertRaises(hass.ToolError) as caught:
            hass.resolve_room("kitchen", needs="covers")
        message = str(caught.exception)
        self.assertIn("kitchen has no covers", message)
        self.assertIn("final", message)
        self.assertIn("living room", caught.exception.extra["rooms_with"])

    def test_an_ambiguous_prefix_asks_rather_than_picking(self):
        hass._map_cache = None
        write_map(TMP, {"rooms": {"bedroom": {"lights": ["light.a"]},
                                  "bedside": {"lights": ["light.b"]}}})
        with self.assertRaises(hass.ToolError) as caught:
            hass.resolve_room("bed")
        self.assertEqual(sorted(caught.exception.extra["candidates"]), ["bedroom", "bedside"])
        write_map(TMP, HOUSE)
        hass._map_cache = None


class TestLights(HassCase):
    def test_a_working_room_confirms(self):
        result = hass.set_lights(room="office", state="on", brightness_pct=40)
        self.assertTrue(result["ok"])
        self.assertIn("confirmed", result["summary"])
        self.assertEqual(len(result["confirmed"]), 2)
        domain, service, payload = self.ha.service_calls[0]
        self.assertEqual((domain, service), ("light", "turn_on"))
        self.assertEqual(payload["brightness_pct"], 40)

    def test_one_dead_bulb_is_a_partial_not_a_success(self):
        # A group with one dead member is what a real house looks like.
        # Reporting "office lights on" here is how people stop trusting this.
        self.ha.deaf.add("light.office_lamp")
        result = hass.set_lights(room="office", state="on")
        self.assertTrue(result["partial"])
        self.assertEqual(result["confirmed"], ["light.office_ceiling"])
        self.assertIn("light.office_lamp", result["summary"])

    def test_a_room_where_nothing_responds_is_a_failure(self):
        self.ha.deaf.update({"light.office_ceiling", "light.office_lamp"})
        result = hass.set_lights(room="office", state="on")
        self.assertFalse(result["ok"])
        self.assertIn("command accepted but nothing changed", result["error"])

    def test_an_unavailable_bulb_is_explained_not_just_reported(self):
        self.ha.states["light.office_lamp"]["state"] = "unavailable"
        self.ha.deaf.add("light.office_lamp")
        result = hass.set_lights(room="office", state="on")
        self.assertIn("off at the switch, or off the mesh", result["summary"])

    def test_colour_temperature_is_verified_not_assumed(self):
        # A bulb that cannot do colour temperature accepts the call and ignores
        # the field. Confirming only brightness would report success for a
        # colour that never changed.
        self.ha.states["light.office_lamp"]["attributes"]["supports_color_temp"] = False
        result = hass.set_lights(room="office", state="on", color_temp_k=2700)
        self.assertTrue(result["partial"])
        self.assertIn("light.office_lamp", result["summary"])
        self.assertIn("fixed-white", result["summary"])

    def test_brightness_with_state_off_is_refused_as_a_contradiction(self):
        with self.assertRaises(hass.ToolError) as caught:
            hass.set_lights(room="office", state="off", brightness_pct=40)
        self.assertIn("if you meant to dim", str(caught.exception))


class TestCovers(HassCase):
    def test_a_middle_position_on_a_positionable_blind(self):
        # "75% closed" is position_pct=25. The tool measures openness.
        result = hass.set_cover(room="office", position_pct=25)
        self.assertTrue(result["ok"])
        self.assertEqual(self.ha.states["cover.office_blind"]["attributes"]["current_position"], 25)
        self.assertIn("25% open", result["summary"])

    def test_a_binary_blind_refuses_the_middle_and_names_the_alternative(self):
        with self.assertRaises(hass.ToolError) as caught:
            hass.set_cover(room="living room", position_pct=25)
        message = str(caught.exception)
        self.assertIn("cover.living_room_roller", message)
        self.assertIn("Use 0 or 100", message)
        self.assertIn("cover.living_room_venetian", caught.exception.extra["positionable"])

    def test_a_binary_blind_accepts_the_endpoints(self):
        result = hass.set_cover(room="living room", position_pct=0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.ha.states["cover.living_room_roller"]["state"], "closed")

    def test_a_missing_entity_is_named_as_missing_not_as_a_simple_blind(self):
        # It used to be sorted in with the two-state covers, producing "it can
        # only open and close" about something that does not exist at all.
        del self.ha.states["cover.bedroom_blackout"]
        with self.assertRaises(hass.ToolError) as caught:
            hass.set_cover(room="bedroom", position_pct=50)
        message = str(caught.exception)
        self.assertIn("exist in Home Assistant", message)
        self.assertIn("The map is wrong", message)
        self.assertNotIn("can only open and close", message)


class TestTilt(HassCase):
    def test_tilting_a_venetian_blind_leaves_its_height_alone(self):
        result = hass.set_cover_tilt(room="living room", tilt_pct=20)
        self.assertTrue(result["ok"])
        attrs = self.ha.states["cover.living_room_venetian"]["attributes"]
        self.assertEqual(attrs["current_tilt_position"], 20)
        self.assertEqual(attrs["current_position"], 100)   # untouched
        self.assertIn("cover.living_room_roller", result["summary"])  # no slats, left alone

    def test_a_room_with_no_slats_is_told_plainly_and_pointed_at_set_cover(self):
        with self.assertRaises(hass.ToolError) as caught:
            hass.set_cover_tilt(room="office", tilt_pct=50)
        message = str(caught.exception)
        self.assertIn("has tilting slats", message)
        self.assertIn("can only move up and down", message)
        self.assertIn("final", message)
        self.assertIn("set_cover", message)
        self.assertIn("living room", caught.exception.extra["rooms_with_tilt"])


class TestThermostat(HassCase):
    def test_a_half_degree_setpoint_is_reachable(self):
        result = hass.set_thermostat(room="office", target=20.5)
        self.assertTrue(result["ok"])
        self.assertEqual(self.ha.states["climate.upstairs"]["attributes"]["temperature"], 20.5)

    def test_the_summary_reports_the_setpoint_and_the_room_separately(self):
        # "Set to 20.5°, currently 17.5°" is true. "The office is 20.5°" is not.
        result = hass.set_thermostat(room="office", target=20.5)
        self.assertIn("setpoint", result["summary"])
        self.assertIn("Currently 17.5°", result["summary"])

    def test_a_thermostat_that_ignores_the_call_is_reported(self):
        self.ha.deaf.add("climate.upstairs")
        result = hass.set_thermostat(room="office", target=22)
        self.assertFalse(result["ok"])
        self.assertIn("setpoint 19°", result["error"])


class TestWholeHouse(HassCase):
    def test_all_covers_closes_every_room_and_confirms(self):
        result = hass.all_covers(position_pct=0)
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["rooms_done"]), ["bedroom", "living room", "office"])
        self.assertEqual(self.ha.states["cover.office_blind"]["attributes"]["current_position"], 0)

    def test_one_stuck_room_is_named_rather_than_folded_in(self):
        self.ha.deaf.add("cover.bedroom_blackout")
        result = hass.all_covers(position_pct=0)
        self.assertTrue(result["partial"])
        self.assertIn("bedroom", result["summary"])
        self.assertIn("office", result["rooms_done"])

    def test_a_room_that_cannot_take_the_position_does_not_stop_the_others(self):
        result = hass.all_covers(position_pct=30)
        self.assertTrue(result["partial"])
        self.assertIn("living room", result["summary"])   # has a binary-only roller
        self.assertIn("office", result["rooms_done"])

    def test_all_lights_off(self):
        hass.set_lights(room="office", state="on")
        result = hass.all_lights(state="off")
        self.assertTrue(result["ok"])
        self.assertEqual(self.ha.states["light.office_ceiling"]["state"], "off")


class TestStatusAndMap(HassCase):
    def test_status_names_entities_home_assistant_does_not_have(self):
        del self.ha.states["light.kitchen_main"]
        result = hass.hass_status()
        self.assertFalse(result["ok"])
        self.assertIn("light.kitchen_main", " ".join(result["missing_entities"]))
        self.assertIn("DO NOT EXIST", result["summary"])

    def test_a_misspelled_room_key_is_caught(self):
        # "light" instead of "lights" maps nothing, the room still resolves,
        # and the bulb is merely absent - which is the hardest kind of wrong.
        hass._map_cache = None
        write_map(TMP, {"rooms": {"office": {"light": ["light.office_ceiling"]}}})
        try:
            result = hass.hass_status()
            self.assertFalse(result["ok"])
            self.assertIn("office.light", " ".join(result["unreadable_map_keys"]))
            self.assertIn("did you mean lights?", " ".join(result["unreadable_map_keys"]))
        finally:
            write_map(TMP, HOUSE)
            hass._map_cache = None

    def test_the_map_reloads_when_the_file_changes(self):
        # Adding a device should not need a restart; the old map would keep
        # working and the new bulb would just be mysteriously missing.
        self.assertNotIn("garage", hass.list_rooms()["rooms"][0].values())
        extended = {"rooms": dict(HOUSE["rooms"], garage={"lights": ["light.garage"]}),
                    "scenes": HOUSE["scenes"]}
        write_map(TMP, extended)
        os.utime(hass.HASS_MAP, (0, 0))    # force a different mtime
        try:
            self.assertIn("garage", [r["room"] for r in hass.list_rooms()["rooms"]])
        finally:
            write_map(TMP, HOUSE)
            hass._map_cache = None

    def test_discovery_refuses_the_deferred_domains(self):
        # Listing locks would only invite an attempt to use them.
        with self.assertRaises(hass.ToolError) as caught:
            hass.discover_entities(domain="lock")
        self.assertIn("FUTURE.md", str(caught.exception))
        self.assertIn("light", caught.exception.extra["addressable_domains"])

    def test_home_status_leads_with_what_is_wrong(self):
        self.ha.states["light.office_ceiling"]["state"] = "unavailable"
        result = hass.home_status()
        self.assertTrue(result["needs_attention"])
        self.assertTrue(result["summary"].split("\n")[1].strip().startswith("!"))


if __name__ == "__main__":
    unittest.main()
