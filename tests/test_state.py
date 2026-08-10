"""state-mcp: dates, recurrence, and the writes that have to be honest.

The date arithmetic gets the most attention here because it is where the
silent wrongness lives. A task due on the wrong day still looks like a working
system; you only find out when the chore does not come back.
"""

import os
import tempfile
import unittest
from datetime import date

from support import load

TMP = tempfile.mkdtemp(prefix="state-mcp-test-")
state = load("state_under_test", "state-mcp/state_mcp_server.py",
             env={"STATE_DB": os.path.join(TMP, "household.db"), "STATE_PERSON": "Nathan"})


class StateCase(unittest.TestCase):
    """Each test gets an empty database and a fixed idea of 'today'."""

    TODAY = date(2026, 3, 10)  # a Tuesday

    def setUp(self):
        state._conn = None
        self.db_path = os.path.join(TMP, f"{self.id().rsplit('.', 1)[-1]}.db")
        state.DB_PATH = self.db_path
        state.today = lambda: self.TODAY
        state.db()

    def tearDown(self):
        if state._conn is not None:
            state._conn.close()
            state._conn = None


class TestDateParsing(StateCase):
    def test_the_words_people_actually_use(self):
        self.assertEqual(state.parse_date("today"), date(2026, 3, 10))
        self.assertEqual(state.parse_date("tomorrow"), date(2026, 3, 11))
        self.assertEqual(state.parse_date("yesterday"), date(2026, 3, 9))
        self.assertEqual(state.parse_date("2026-12-25"), date(2026, 12, 25))

    def test_a_weekday_always_means_the_next_one(self):
        # Today is Tuesday. "Tuesday" has to mean next Tuesday, not today -
        # a task due "Tuesday" said on a Tuesday is never about this morning.
        self.assertEqual(state.parse_date("tuesday"), date(2026, 3, 17))
        self.assertEqual(state.parse_date("friday"), date(2026, 3, 13))

    def test_relative_spans(self):
        self.assertEqual(state.parse_date("+3 days"), date(2026, 3, 13))
        self.assertEqual(state.parse_date("in 2 weeks"), date(2026, 3, 24))
        self.assertEqual(state.parse_date("1 month"), date(2026, 4, 10))

    def test_an_unreadable_date_says_what_would_work(self):
        with self.assertRaises(state.ToolError) as caught:
            state.parse_date("sometime next week-ish", "due")
        message = str(caught.exception)
        self.assertIn("due=", message)
        self.assertIn("YYYY-MM-DD", message)

    def test_empty_is_no_date_rather_than_an_error(self):
        self.assertIsNone(state.parse_date(""))
        self.assertIsNone(state.parse_date(None))


class TestIntervals(StateCase):
    def test_month_end_clamps_instead_of_overflowing(self):
        self.assertEqual(state.add_interval(date(2025, 1, 31), 1, "month"), date(2025, 2, 28))
        self.assertEqual(state.add_interval(date(2024, 1, 31), 1, "month"), date(2024, 2, 29))
        self.assertEqual(state.add_interval(date(2025, 3, 31), 1, "month"), date(2025, 4, 30))

    def test_a_yearly_interval_from_a_leap_day_does_not_raise(self):
        # This one used to throw ValueError out of task_complete, after the
        # task had already been marked done - so the chore silently stopped
        # recurring and nothing looked broken.
        self.assertEqual(state.add_interval(date(2024, 2, 29), 1, "year"), date(2025, 2, 28))
        self.assertEqual(state.add_interval(date(2024, 2, 29), 4, "year"), date(2028, 2, 29))

    def test_crossing_a_year_boundary(self):
        self.assertEqual(state.add_interval(date(2025, 11, 30), 3, "month"), date(2026, 2, 28))
        self.assertEqual(state.add_interval(date(2025, 12, 31), 1, "month"), date(2026, 1, 31))

    def test_named_recurrences(self):
        self.assertEqual(state.parse_recurrence("quarterly"), (3, "month"))
        self.assertEqual(state.parse_recurrence("fortnightly"), (2, "week"))
        self.assertEqual(state.parse_recurrence("every 3 months"), (3, "month"))
        self.assertIsNone(state.parse_recurrence(""))

    def test_a_recurrence_that_looks_like_a_try_is_refused_early(self):
        # Validated at task_add, not at completion: a task that only reveals its
        # bad recurrence months later has already stopped recurring by then.
        with self.assertRaises(state.ToolError):
            state.parse_recurrence("whenever it looks grubby")


class TestTasks(StateCase):
    def test_recurrence_anchors_on_completion_not_the_old_due_date(self):
        state.task_add(title="change furnace filter", due="2026-02-01", recurrence="quarterly")
        result = state.task_complete(task_id=1, actor="Nathan")
        # Completed six weeks late, so the next one is three months from today.
        self.assertEqual(result["next_task"]["due"], "2026-06-10")

    def test_completing_a_leap_day_yearly_task_creates_the_next_one(self):
        state.today = lambda: date(2028, 2, 29)
        state.task_add(title="service the boiler", recurrence="yearly")
        result = state.task_complete(task_id=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["next_task"]["due"], "2029-02-28")

    def test_a_failed_recurrence_leaves_the_task_open(self):
        # The two writes are one operation. If the follow-on cannot be created,
        # the completion must roll back rather than leave a recurring task done
        # with nothing scheduled to replace it.
        state.task_add(title="clean gutters", recurrence="yearly")
        original = state.add_interval

        def explode(*args, **kwargs):
            raise RuntimeError("clock is on fire")

        state.add_interval = explode
        try:
            with self.assertRaises(RuntimeError):
                state.task_complete(task_id=1)
        finally:
            state.add_interval = original
        still_open = state.q("SELECT status FROM tasks WHERE id=1")[0]["status"]
        self.assertEqual(still_open, "open")

    def test_completing_twice_is_reported_not_repeated(self):
        state.task_add(title="water the plants")
        state.task_complete(task_id=1, actor="Sam")
        again = state.task_complete(task_id=1, actor="Nathan")
        self.assertTrue(again["ok"])
        self.assertIn("already completed by Sam", again["summary"])
        self.assertIsNone(again.get("next_task"))

    def test_an_unknown_id_names_the_open_tasks(self):
        state.task_add(title="clean gutters")
        with self.assertRaises(state.ToolError) as caught:
            state.task_complete(task_id=99)
        self.assertIn("#1 clean gutters", caught.exception.extra["open_tasks"])

    def test_an_unknown_area_is_refused_with_the_vocabulary(self):
        with self.assertRaises(state.ToolError) as caught:
            state.task_add(title="tidy", area="the shed")
        self.assertIn("house", caught.exception.extra["known_areas"])


class TestTaskUpdate(StateCase):
    def test_only_the_named_fields_change(self):
        state.task_add(title="call the plumber", area="admin", assignee="Sam", due="today")
        state.task_update(task_id=1, due="+7 days")
        row = state.q("SELECT * FROM tasks WHERE id=1")[0]
        self.assertEqual(row["due"], "2026-03-17")
        self.assertEqual(row["assignee"], "Sam")     # untouched
        self.assertEqual(row["area"], "admin")       # untouched
        self.assertEqual(row["title"], "call the plumber")

    def test_none_clears_a_field(self):
        state.task_add(title="call the plumber", assignee="Sam", due="today")
        state.task_update(task_id=1, assignee="none", due="none")
        row = state.q("SELECT * FROM tasks WHERE id=1")[0]
        self.assertIsNone(row["assignee"])
        self.assertIsNone(row["due"])

    def test_a_bad_recurrence_is_refused_before_it_is_stored(self):
        state.task_add(title="clean gutters", recurrence="yearly")
        with self.assertRaises(state.ToolError):
            state.task_update(task_id=1, recurrence="when it rains")
        self.assertEqual(state.q("SELECT recurrence FROM tasks WHERE id=1")[0]["recurrence"],
                         "yearly")

    def test_editing_a_finished_task_is_refused(self):
        state.task_add(title="clean gutters")
        state.task_complete(task_id=1)
        with self.assertRaises(state.ToolError) as caught:
            state.task_update(task_id=1, title="clean the gutters properly")
        self.assertIn("not open", str(caught.exception))

    def test_changing_nothing_says_so(self):
        state.task_add(title="clean gutters")
        with self.assertRaises(state.ToolError) as caught:
            state.task_update(task_id=1)
        self.assertIn("Nothing to change", str(caught.exception))


class TestShoppingAndPantry(StateCase):
    def test_a_duplicate_is_reported_not_added_twice(self):
        state.shopping_add(item="olive oil", actor="Sam")
        again = state.shopping_add(item="Olive Oil", actor="Nathan")
        self.assertTrue(again["duplicate"])
        self.assertIn("added by Sam", again["summary"])
        self.assertEqual(len(state.shopping_list()["items"]), 1)

    def test_buying_a_staple_restocks_the_pantry(self):
        state.pantry_set(item="olive oil", qty=0, staple=True, threshold=1)
        self.assertEqual([r["item"] for r in state.pantry_low()["items"]], ["olive oil"])
        state.shopping_add(item="olive oil")
        state.shopping_bought(items="olive oil", actor="Sam")
        self.assertEqual(state.pantry_low()["items"], [])

    def test_removing_a_typo_does_not_restock_anything(self):
        # The reason shopping_remove exists. Marking a typo "bought" would tell
        # the house it has something it does not.
        state.pantry_set(item="olive oil", qty=0, staple=True, threshold=1)
        state.shopping_add(item="olive oil")
        state.shopping_remove(items="olive oil", reason="typo")
        self.assertEqual(state.shopping_list()["items"], [])
        self.assertEqual([r["item"] for r in state.pantry_low()["items"]], ["olive oil"])

    def test_removing_something_not_on_the_list_shows_the_list(self):
        state.shopping_add(item="milk")
        with self.assertRaises(state.ToolError) as caught:
            state.shopping_remove(items="caviar")
        self.assertEqual(caught.exception.extra["on_list"], ["milk"])

    def test_pantry_remove_warns_when_it_was_a_staple(self):
        state.pantry_set(item="olive oil", qty=2, staple=True)
        result = state.pantry_remove(item="olive oil")
        self.assertIn("nothing will warn", result["summary"])
        self.assertEqual(state.q("SELECT COUNT(*) c FROM pantry")[0]["c"], 0)

    def test_pantry_remove_on_an_unknown_item_lists_what_is_there(self):
        state.pantry_set(item="flour", qty=1)
        with self.assertRaises(state.ToolError) as caught:
            state.pantry_remove(item="saffron")
        self.assertEqual(caught.exception.extra["in_pantry"], ["flour"])

    def test_quantity_must_be_a_number(self):
        with self.assertRaises(state.ToolError) as caught:
            state.pantry_set(item="rice", qty="a lot")
        self.assertIn("unit field", str(caught.exception))


class TestAppointments(StateCase):
    def test_cancelling_removes_it_and_leaves_a_trail(self):
        state.appointment_add(what="vet", date="+2 days", who="Sam")
        result = state.appointment_cancel(appointment_id=1, reason="rescheduled")
        self.assertTrue(result["ok"])
        self.assertEqual(state.appointment_list()["appointments"], [])
        trail = state.q("SELECT * FROM journal WHERE action='appointment_cancel'")
        self.assertEqual(len(trail), 1)
        self.assertIn("rescheduled", trail[0]["detail"])

    def test_cancelling_an_unknown_id_lists_the_upcoming_ones(self):
        state.appointment_add(what="dentist", date="+1 day")
        with self.assertRaises(state.ToolError) as caught:
            state.appointment_cancel(appointment_id=42)
        self.assertIn("#1 dentist", " ".join(caught.exception.extra["upcoming"]))

    def test_a_malformed_time_is_refused(self):
        with self.assertRaises(state.ToolError) as caught:
            state.appointment_add(what="vet", date="today", time="half past three")
        self.assertIn("HH:MM", str(caught.exception))


class TestPeople(StateCase):
    def test_aliases_resolve_to_the_canonical_name(self):
        state.person_add(name="Nathan", aliases="Nate, cook")
        self.assertEqual(state.resolve_person("nate"), "Nathan")
        self.assertEqual(state.resolve_person("COOK"), "Nathan")

    def test_an_unknown_name_is_registered_and_flagged(self):
        notes = []
        self.assertEqual(state.resolve_person("Wilhelmina", notes), "Wilhelmina")
        self.assertIn("not on the roster", notes[0])
        self.assertIn("Wilhelmina", [r["name"] for r in state.q("SELECT name FROM people")])


class TestDigest(StateCase):
    def test_a_quiet_house_says_nothing_needs_attention(self):
        digest = state.household_digest()
        self.assertFalse(digest["needs_attention"])
        self.assertIn("nothing needs attention", digest["summary"])

    def test_overdue_work_leads_and_sets_the_flag(self):
        state.task_add(title="clean gutters", due="2026-01-01")
        digest = state.household_digest()
        self.assertTrue(digest["needs_attention"])
        self.assertIn("overdue", digest["summary"])
        self.assertEqual(len(digest["overdue"]), 1)

    def test_unfiled_captures_are_surfaced(self):
        state.capture_add(raw="get the thing for the sink", source="telegram")
        digest = state.household_digest()
        self.assertTrue(digest["needs_attention"])
        self.assertEqual(digest["unfiled_captures"], 1)


class TestJournal(StateCase):
    def test_failures_sort_ahead_of_successes(self):
        state.journal_record(action="set_lights", target="office", outcome="ok")
        state.journal_record(action="set_cover", target="bedroom", outcome="failed")
        review = state.journal_review(days=1)
        self.assertEqual(len(review["problems"]), 1)
        self.assertTrue(review["summary"].startswith("1 problem"))
        self.assertIn("FAILED", review["summary"].split("|")[0])

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(state.ToolError):
            state.journal_record(action="set_lights", outcome="probably fine")


if __name__ == "__main__":
    unittest.main()
