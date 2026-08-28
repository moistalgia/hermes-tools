"""create_automation authors a NATIVE Home Assistant automation. The whole
point is that a write here is never trusted at face value - see
../AUTOMATIONS_DESIGN.md §2.4. These tests exercise the failure modes that
would otherwise fail quietly: a saved config with no matching entity, an
action step Home Assistant would accept but a real device can't perform, and
the ownership bookkeeping (hermes-managed label, deterministic id) that keeps
"maintain" from silently duplicating things.
"""

import os
import sys
import unittest

os.environ.setdefault("HASS_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_mcp.server as ha  # noqa: E402
from fake_ha import FakeHomeAssistant  # noqa: E402


class AutomationCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHomeAssistant()
        (self.fake
         .entity("binary_sensor.hallway_motion", "off")
         .entity("light.hallway", "off", supported_color_modes=["brightness"])
         .entity("light.porch", "off", supported_color_modes=["onoff"])  # not dimmable
         .entity("cover.bedroom_blind", "open", current_position=100,
                 supported_features=15)  # open|close|set_position|stop
         .entity("scene.movie_night", "2026-01-01T00:00:00"))
        self.fake.install(ha)


class TestCreateAutomation(AutomationCase):
    def test_motion_to_light_is_created_and_confirmed(self):
        result = ha.create_automation(
            alias="Hallway motion -> light",
            triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on", "brightness_pct": 60}],
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["confirmed"], result)
        self.assertTrue(result["created"])
        self.assertEqual(result["automation_id"], "hermes_hallway_motion_light")
        self.assertEqual(result["state"], "on")

        saved = self.fake.automations["hermes_hallway_motion_light"]
        self.assertEqual(saved["triggers"], [
            {"trigger": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}])
        self.assertEqual(saved["actions"], [
            {"action": "light.turn_on", "target": {"entity_id": "light.hallway"},
             "data": {"brightness_pct": 60}}])

        # Ownership bookkeeping: the entity this created is labeled, not guessed.
        reg = self.fake.entity_registry[result["entity_id"]]
        self.assertIn("hermes-managed", reg["labels"])

    def test_sunrise_with_offset_and_cover_action(self):
        result = ha.create_automation(
            alias="Sunrise blinds",
            triggers=[{"kind": "sun", "event": "sunrise", "offset_minutes": -30}],
            actions=[{"kind": "cover", "entity_id": "cover.bedroom_blind", "action": "position", "position": 70}],
        )
        self.assertTrue(result["ok"], result)
        saved = self.fake.automations[result["automation_id"]]
        self.assertEqual(saved["triggers"], [{"trigger": "sun", "event": "sunrise", "offset": "-00:30:00"}])
        self.assertEqual(saved["actions"], [
            {"action": "cover.set_cover_position", "target": {"entity_id": "cover.bedroom_blind"},
             "data": {"position": 70}}])

    def test_created_disabled_when_asked(self):
        result = ha.create_automation(
            alias="Draft automation",
            triggers=[{"kind": "time", "at": "07:00:00"}],
            actions=[{"kind": "scene", "entity_id": "scene.movie_night"}],
            enabled=False,
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["state"], "off")
        domain_service = [(d, s) for d, s, _ in self.fake.service_calls]
        self.assertIn(("automation", "turn_off"), domain_service)

    def test_calling_again_with_same_alias_updates_not_duplicates(self):
        first = ha.create_automation(
            alias="Hallway motion -> light",
            triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on"}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on"}],
        )
        second = ha.create_automation(
            alias="Hallway motion -> light",
            triggers=[{"kind": "state", "entity_id": "binary_sensor.hallway_motion", "to": "on",
                       "for_seconds": 5}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on", "brightness_pct": 80}],
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["automation_id"], second["automation_id"])
        self.assertEqual(first["entity_id"], second["entity_id"])
        self.assertEqual(len(self.fake.automations), 1)
        automation_entities = [e for e in self.fake.states if e.startswith("automation.")]
        self.assertEqual(len(automation_entities), 1)  # updated in place, no orphaned duplicate

    def test_unsupported_action_kind_is_refused_before_saving(self):
        result = ha.create_automation(
            alias="Should never save",
            triggers=[{"kind": "time", "at": "22:00:00"}],
            actions=[{"kind": "lock", "entity_id": "lock.front_door", "action": "unlock"}],
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown action kind", result["error"])
        self.assertNotIn("hermes_should_never_save", self.fake.automations)

    def test_dimming_a_non_dimmable_light_is_refused_before_saving(self):
        result = ha.create_automation(
            alias="Porch at sunset",
            triggers=[{"kind": "sun", "event": "sunset"}],
            actions=[{"kind": "light", "entity_id": "light.porch", "action": "on", "brightness_pct": 50}],
        )
        self.assertFalse(result["ok"])
        self.assertIn("not dimmable", result["error"])
        self.assertEqual(self.fake.automations, {})

    def test_trigger_entity_that_does_not_exist_is_refused(self):
        result = ha.create_automation(
            alias="Ghost sensor",
            triggers=[{"kind": "state", "entity_id": "binary_sensor.does_not_exist", "to": "on"}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on"}],
        )
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["error"])

    def test_empty_triggers_or_actions_is_refused(self):
        self.assertFalse(ha.create_automation(alias="x", triggers=[], actions=[{"kind": "scene", "entity_id": "scene.movie_night"}])["ok"])
        self.assertFalse(ha.create_automation(alias="x", triggers=[{"kind": "time", "at": "07:00:00"}], actions=[])["ok"])


class TestAutomationCommand(AutomationCase):
    def _create(self):
        return ha.create_automation(
            alias="Test automation",
            triggers=[{"kind": "time", "at": "07:00:00"}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on"}],
        )

    def test_trigger_runs_now_and_updates_last_triggered(self):
        created = self._create()
        result = ha.automation_command(created["automation_id"], "trigger")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["confirmed"])
        self.assertIsNotNone(result["last_triggered"])

    def test_disable_then_enable_round_trips(self):
        created = self._create()
        off = ha.automation_command(created["automation_id"], "disable")
        self.assertTrue(off["confirmed"])
        self.assertEqual(off["state"], "off")
        on = ha.automation_command(created["automation_id"], "enable")
        self.assertTrue(on["confirmed"])
        self.assertEqual(on["state"], "on")

    def test_delete_returns_full_config_for_recreation(self):
        created = self._create()
        result = ha.automation_command(created["automation_id"], "delete")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["deleted_config"]["alias"], "Test automation")
        self.assertNotIn(created["automation_id"], self.fake.automations)
        # And it is gone from discovery.
        listed = ha.list_automations(managed_only=False)
        self.assertNotIn(created["automation_id"], [a["automation_id"] for a in listed["automations"]])

    def test_unknown_automation_id_is_refused(self):
        result = ha.automation_command("hermes_does_not_exist", "trigger")
        self.assertFalse(result["ok"])
        self.assertIn("no automation", result["error"])


class TestListAndGetAutomations(AutomationCase):
    def test_managed_only_hides_hand_written_automations(self):
        ha.create_automation(
            alias="Hermes-made",
            triggers=[{"kind": "time", "at": "07:00:00"}],
            actions=[{"kind": "light", "entity_id": "light.hallway", "action": "on"}],
        )
        # Simulate an automation nobody made through this server.
        self.fake.automations["manual_one"] = {"alias": "Hand written", "triggers": [], "actions": []}
        self.fake.entity("automation.hand_written", "on", id="manual_one", friendly_name="Hand written")

        managed = ha.list_automations()
        self.assertEqual([a["automation_id"] for a in managed["automations"]], ["hermes_hermes_made"])

        everything = ha.list_automations(managed_only=False)
        self.assertEqual(everything["count"], 2)

    def test_get_automation_round_trips_the_structured_shape(self):
        ha.create_automation(
            alias="Round trip",
            triggers=[{"kind": "numeric_state", "entity_id": "binary_sensor.hallway_motion", "below": 40}],
            actions=[{"kind": "delay", "seconds": 3}],
        )
        got = ha.get_automation("hermes_round_trip")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["alias"], "Round trip")
        self.assertEqual(got["actions"], [{"delay": {"seconds": 3.0}}])
        self.assertEqual(got["state"], "on")


if __name__ == "__main__":
    unittest.main()
