"""Areas have no REST API in Home Assistant - every call here goes over the
WebSocket transport (_ws_call), which is otherwise untested by the rest of
this suite. See ../AUTOMATIONS_DESIGN.md §4-5.
"""

import os
import sys
import unittest

os.environ.setdefault("HASS_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_mcp.server as ha  # noqa: E402
from fake_ha import FakeHomeAssistant  # noqa: E402

# Captured before any test below monkeypatches ha._ws_call, so TestNoToken
# exercises the real transport function regardless of test execution order.
_REAL_WS_CALL = ha._ws_call


class AreaCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHomeAssistant()
        self.fake.entity("light.theater_lamp", "off")
        self.office = self.fake.area("Office", aliases=["study", "back room"])
        self.fake.install(ha)


class TestListAreas(AreaCase):
    def test_lists_name_and_aliases(self):
        result = ha.list_areas()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["areas"][0]["name"], "Office")
        self.assertEqual(result["areas"][0]["aliases"], ["study", "back room"])


class TestCreateArea(AreaCase):
    def test_creates_a_new_area(self):
        result = ha.create_area("Theater", aliases=["home cinema"])
        self.assertTrue(result["ok"], result)
        self.assertIn(result["area_id"], self.fake.areas)
        self.assertEqual(self.fake.areas[result["area_id"]]["aliases"], ["home cinema"])

    def test_refuses_a_duplicate_name(self):
        result = ha.create_area("Office")
        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["error"])
        self.assertEqual(result.get("area_id"), self.office)

    def test_refuses_a_matching_alias(self):
        result = ha.create_area("study")
        self.assertFalse(result["ok"])
        self.assertIn("already exists", result["error"])

    def test_empty_name_is_refused(self):
        self.assertFalse(ha.create_area("")["ok"])


class TestAssignArea(AreaCase):
    def test_assigns_and_confirms(self):
        result = ha.assign_area("light.theater_lamp", "office")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["area_id"], self.office)
        self.assertEqual(self.fake.entity_registry["light.theater_lamp"]["area_id"], self.office)

    def test_matches_by_alias_case_insensitively(self):
        result = ha.assign_area("light.theater_lamp", "STUDY")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["area_id"], self.office)

    def test_unknown_area_is_refused_with_known_list(self):
        result = ha.assign_area("light.theater_lamp", "garage")
        self.assertFalse(result["ok"])
        self.assertIn("Office", result["known_areas"])

    def test_ambiguous_area_returns_candidates_not_a_guess(self):
        # Neither name equals the query exactly, so an exact match can't
        # break the tie - both are plausible and neither should be guessed.
        self.fake.area("Guest Room")
        self.fake.area("Guest Bathroom")
        result = ha.assign_area("light.theater_lamp", "guest")
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result.get("candidates"))
        self.assertEqual(sorted(result["candidates"]), ["Guest Bathroom", "Guest Room"])

    def test_nonexistent_entity_is_refused(self):
        result = ha.assign_area("light.does_not_exist", "office")
        self.assertFalse(result["ok"])


class TestNoToken(unittest.TestCase):
    def test_ws_call_without_a_token_fails_cleanly_with_no_socket(self):
        original = ha.TOKEN
        ha.TOKEN = ""
        try:
            result = _REAL_WS_CALL("config/area_registry/list")
        finally:
            ha.TOKEN = original
        self.assertFalse(result["ok"])
        self.assertIn("HASS_TOKEN", result["error"])


if __name__ == "__main__":
    unittest.main()
