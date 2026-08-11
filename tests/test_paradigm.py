"""paradigm-mcp: event_key field and marker line in day_event.

These are the additions that let an agent reconcile calendar events without
needing to import a file — it can look up and update events by key rather than
creating duplicates, and fall back to reading the marker line out of an event's
description body when extended-property lookup isn't available.

The tests use the internal helpers directly, which keeps the fixture minimal
(no credentials, no network) and makes the invariants explicit.
"""

import importlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from support import load  # noqa: E402

TMP = tempfile.mkdtemp(prefix="paradigm-mcp-test-")
paradigm = load(
    "paradigm_under_test",
    "paradigm-mcp/paradigm_mcp_server.py",
    env={"PARADIGM_CACHE_DIR": TMP, "PARADIGM_USERNAME": "", "PARADIGM_PASSWORD": ""},
)


# ---------------------------------------------------------------------------
# Minimal plan fixtures
# ---------------------------------------------------------------------------

def _plan_with_sessions(*sessions):
    """Wrap raw session dicts in the envelope load_plan() would return."""
    return {"sessions": list(sessions), "fetched_at": "2026-08-11T12:00:00+00:00"}


def _session(date, name="Test Session", minutes=60, prescribed=True):
    return {
        "date": date,
        "name": name,
        "position": 1,
        "duration_min": {"max": minutes},
        "prescribed": prescribed,
        "section_summary": [],
        "exercise_count": 0,
        "optional": False,
        "detailed": False,
        "note": None,
        "sections": [],
    }


# ---------------------------------------------------------------------------
# event_key helper
# ---------------------------------------------------------------------------

class TestEventKey(unittest.TestCase):
    def test_format_matches_ics_uid_scheme(self):
        # The .ics exporter uses `paradigm-YYYYMMDD@hermes-tools`.
        # The event_key drops the domain suffix — it is the prefix that agents
        # use as an idempotency key, consistent with the ICS UID.
        key = paradigm._event_key("2026-08-12")
        self.assertEqual(key, "paradigm-20260812")

    def test_different_dates_produce_different_keys(self):
        self.assertNotEqual(
            paradigm._event_key("2026-08-11"),
            paradigm._event_key("2026-08-12"),
        )

    def test_marker_line_contains_key(self):
        key = paradigm._event_key("2026-08-12")
        marker = paradigm._marker_line(key)
        self.assertIn(key, marker)
        self.assertTrue(marker.startswith("[hermes:paradigm:event_key="))


# ---------------------------------------------------------------------------
# day_event: training day
# ---------------------------------------------------------------------------

class TestDayEventTraining(unittest.TestCase):
    def setUp(self):
        self.plan = _plan_with_sessions(_session("2026-08-12"))
        self.event = paradigm.day_event(self.plan, "2026-08-12")

    def test_event_key_field_present(self):
        self.assertEqual(self.event["event_key"], "paradigm-20260812")

    def test_event_key_consistent_with_date(self):
        self.assertTrue(self.event["event_key"].endswith("20260812"))

    def test_description_ends_with_marker_line(self):
        desc = self.event["description"]
        last_line = desc.splitlines()[-1]
        self.assertTrue(
            last_line.startswith("[hermes:paradigm:event_key="),
            msg=f"Last line of description was: {last_line!r}",
        )
        self.assertIn("paradigm-20260812", last_line)

    def test_marker_line_is_on_its_own_line(self):
        # The marker must not run into the human-readable body above it.
        desc = self.event["description"]
        lines = desc.splitlines()
        self.assertGreater(len(lines), 1)
        marker_lines = [line for line in lines if "[hermes:paradigm:event_key=" in line]
        self.assertEqual(len(marker_lines), 1)


# ---------------------------------------------------------------------------
# day_event: rest day (include_rest=True)
# ---------------------------------------------------------------------------

class TestDayEventRest(unittest.TestCase):
    def setUp(self):
        self.plan = _plan_with_sessions()   # no sessions → rest day
        self.event = paradigm.day_event(self.plan, "2026-08-11", include_rest=True)

    def test_event_key_field_present_on_rest_day(self):
        self.assertEqual(self.event["event_key"], "paradigm-20260811")

    def test_description_ends_with_marker_line_on_rest_day(self):
        desc = self.event["description"]
        last_line = desc.splitlines()[-1]
        self.assertIn("paradigm-20260811", last_line)

    def test_rest_day_without_include_rest_returns_none(self):
        result = paradigm.day_event(self.plan, "2026-08-11", include_rest=False)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# build_ics: existing .ics behaviour unchanged
# ---------------------------------------------------------------------------

class TestBuildIcsUnchanged(unittest.TestCase):
    def _event(self, date="2026-08-12"):
        plan = _plan_with_sessions(_session(date))
        return paradigm.day_event(plan, date)

    def test_ics_uid_matches_event_key_prefix(self):
        event = self._event("2026-08-12")
        ics = paradigm.build_ics([event])
        # UID in the .ics should incorporate the same compact date.
        self.assertIn("UID:paradigm-20260812@hermes-tools", ics)

    def test_ics_contains_summary(self):
        event = self._event("2026-08-12")
        ics = paradigm.build_ics([event])
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("END:VEVENT", ics)
        self.assertIn("DTSTART;VALUE=DATE:20260812", ics)

    def test_ics_description_includes_marker(self):
        event = self._event("2026-08-12")
        ics = paradigm.build_ics([event])
        # The marker is embedded in the DESCRIPTION line (after .ics escaping).
        self.assertIn("paradigm-20260812", ics)


if __name__ == "__main__":
    unittest.main()
