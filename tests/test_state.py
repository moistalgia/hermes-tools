"""state-mcp: dates, recurrence, identity, and the writes that have to be honest.

The date arithmetic gets the most attention here because it is where the
silent wrongness lives. A task due on the wrong day still looks like a working
system; you only find out when the chore does not come back.

Attribution is the same shape of problem. One bot serves the whole house, so a
write credited to the wrong person is not an error anyone sees - it is a
shopping list that quietly says the wrong thing about who wanted what. Hence
`STATE_PERSON` deliberately set below: the tests prove it is ignored.
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
        state.pantry_set(item="sriracha", qty=2, staple=True)
        result = state.pantry_remove(item="sriracha")
        self.assertIn("nothing will warn", result["summary"])
        self.assertEqual(state.q("SELECT COUNT(*) c FROM pantry WHERE item='sriracha'")[0]["c"], 0)

    def test_pantry_remove_on_an_unknown_item_lists_only_real_stock(self):
        # The seeded assumed staples are a standing decision not to shop for
        # something, not stock. Offering them here would suggest deleting salt
        # to fix a typo about flour.
        state.pantry_set(item="sriracha", qty=1)
        with self.assertRaises(state.ToolError) as caught:
            state.pantry_remove(item="saffron")
        self.assertEqual(caught.exception.extra["in_pantry"], ["sriracha"])

    def test_quantity_must_be_a_number(self):
        with self.assertRaises(state.ToolError) as caught:
            state.pantry_set(item="rice", qty="a lot")
        self.assertIn("unit field", str(caught.exception))


class TestIngredients(StateCase):
    """Turning a recipe line into something you can look for in a cupboard."""

    def test_measurements_and_prep_are_stripped(self):
        self.assertEqual(state.normalize_ingredient("2 tbsp olive oil"), "olive oil")
        self.assertEqual(state.normalize_ingredient("400g spaghetti"), "spaghetti")
        self.assertEqual(state.normalize_ingredient("3 cloves garlic, minced"), "garlic")
        self.assertEqual(state.normalize_ingredient("1 1/2 cups flour"), "flour")
        self.assertEqual(state.normalize_ingredient("a pinch of saffron"), "saffron")
        self.assertEqual(state.normalize_ingredient("4 large eggs"), "eggs")
        self.assertEqual(state.normalize_ingredient("1 onion (finely chopped)"), "onion")

    def test_a_qualifier_still_matches_but_a_different_product_does_not(self):
        # Suffix matching alone reads as obviously correct and is wrong for
        # exactly the ingredients worth getting right. "extra virgin" describes
        # olive oil; "rice" makes vinegar a different bottle.
        self.assertTrue(state.ingredient_matches("olive oil", "extra virgin olive oil"))
        self.assertTrue(state.ingredient_matches("salt", "sea salt"))
        self.assertTrue(state.ingredient_matches("pepper", "black pepper"))
        self.assertFalse(state.ingredient_matches("vinegar", "rice vinegar"))
        self.assertFalse(state.ingredient_matches("butter", "peanut butter"))
        self.assertFalse(state.ingredient_matches("rice", "rice vinegar"))
        self.assertFalse(state.ingredient_matches("sesame oil", "olive oil"))

    def test_a_prepared_form_is_its_own_product(self):
        # "chopped" is what you do to the onion after you get home. "ground",
        # "diced" and "grated" are things you buy as such, and flattening them
        # sends someone to the wrong shelf - or worse, spares the ingredient
        # entirely because the pantry has the unprepared version.
        self.assertEqual(state.normalize_ingredient("1 onion, finely chopped"), "onion")
        self.assertEqual(state.normalize_ingredient("500g ground beef"), "ground beef")
        self.assertEqual(state.normalize_ingredient("1 can diced tomatoes"), "diced tomatoes")
        self.assertFalse(state.ingredient_matches("beef", "ground beef"))
        self.assertFalse(state.ingredient_matches("tomatoes", "diced tomatoes"))
        self.assertTrue(state.ingredient_matches("beef", "beef"))


class TestRecipeShopping(StateCase):
    def test_kitchen_staples_do_not_reach_the_shopping_list(self):
        # The whole point. A list of nineteen items including salt gets
        # ignored, and then you have lost the list itself.
        result = state.shopping_add_recipe(
            dish="carbonara",
            ingredients="400g spaghetti\n200g guanciale, diced\n4 large eggs\n"
                        "2 tbsp extra virgin olive oil\nsalt and pepper to taste")
        self.assertEqual(result["to_buy"], ["spaghetti", "guanciale", "eggs"])
        self.assertEqual(result["assumed"], ["extra virgin olive oil", "salt", "pepper"])
        self.assertIn("Assumed you have", result["summary"])

    def test_something_in_the_pantry_is_not_bought_again(self):
        state.pantry_set(item="rice", qty=5, staple=True, threshold=1)
        result = state.shopping_add_recipe(ingredients="300g rice; 2 chicken breasts")
        self.assertEqual(result["to_buy"], ["chicken breasts"])
        self.assertEqual(result["in_pantry"], ["rice (5)"])

    def test_a_tracked_staple_that_is_out_goes_on_the_list(self):
        # A measurement outranks an assumption. Having a row for soy sauce that
        # says zero is strictly better information than assuming a kitchen.
        state.pantry_set(item="soy sauce", qty=0, staple=True, threshold=1)
        result = state.shopping_add_recipe(ingredients="3 tbsp soy sauce")
        self.assertEqual(result["to_buy"], ["soy sauce"])

    def test_preview_writes_nothing(self):
        result = state.shopping_add_recipe(ingredients="400g spaghetti", preview=True)
        self.assertEqual(result["to_buy"], ["spaghetti"])
        self.assertIn("Would add", result["summary"])
        self.assertEqual(state.shopping_list()["items"], [])

    def test_something_already_on_the_list_is_not_duplicated(self):
        state.shopping_add(item="spaghetti")
        result = state.shopping_add_recipe(ingredients="400g spaghetti; 4 eggs")
        self.assertEqual(result["to_buy"], ["eggs"])
        self.assertEqual(result["already_listed"], ["spaghetti"])
        self.assertEqual(len(state.shopping_list()["items"]), 2)

    def test_the_list_says_which_dish_an_item_is_for(self):
        state.shopping_add_recipe(dish="carbonara", ingredients="200g guanciale")
        self.assertIn("for carbonara", state.shopping_list()["summary"])

    def test_the_same_ingredient_twice_is_added_once(self):
        result = state.shopping_add_recipe(ingredients="2 eggs; 4 large eggs")
        self.assertEqual(result["to_buy"], ["eggs"])

    def test_an_empty_recipe_says_what_it_wanted(self):
        with self.assertRaises(state.ToolError) as caught:
            state.shopping_add_recipe(ingredients="   ")
        self.assertIn("ingredient list", str(caught.exception))

    def test_assumptions_survive_a_quantity_update(self):
        # `assumed` is a standing decision about an item, not an observation of
        # it, so recording how much salt is in the jar must not cancel it.
        state.pantry_set(item="salt", qty=2, location="pantry")
        self.assertEqual(state.q("SELECT assumed FROM pantry WHERE item='salt'")[0]["assumed"], 1)
        state.pantry_set(item="salt", assumed=False)
        result = state.shopping_add_recipe(ingredients="1 tsp salt")
        self.assertEqual(result["to_buy"], ["salt"])

    def test_seeding_happens_once_and_not_on_an_existing_database(self):
        # Re-seeding would resurrect items the household had deliberately
        # removed, and salt would reappear months later with nobody able to
        # say why.
        state.pantry_remove(item="salt")
        state._conn.close()
        state._conn = None
        state.db()
        self.assertEqual(state.q("SELECT COUNT(*) c FROM pantry WHERE item='salt'")[0]["c"], 0)


class TestHistory(StateCase):
    def test_who_did_what(self):
        state.person_add(name="Nick")
        state.person_add(name="Sarah")
        state.task_add(title="mow the lawn")
        state.task_add(title="clean gutters")
        state.task_complete(task_id=1, actor="Nick")
        state.task_complete(task_id=2, actor="Sarah")
        state.shopping_add(item="milk")
        state.shopping_bought(all=True, actor="Nick")
        state.meal_plan(date="today", dish="risotto", cook="Sarah")

        history = state.household_history()
        self.assertEqual([t["title"] for t in history["chores"]],
                         ["clean gutters", "mow the lawn"])
        self.assertEqual(history["by_person"]["Nick"], {"chores": 1, "shopping": 1, "meals": 0})
        self.assertEqual(history["by_person"]["Sarah"], {"chores": 1, "shopping": 0, "meals": 1})

    def test_one_person_only(self):
        state.task_add(title="mow the lawn")
        state.task_add(title="clean gutters")
        state.task_complete(task_id=1, actor="Nick")
        state.task_complete(task_id=2, actor="Sarah")
        history = state.household_history(person="Nick")
        self.assertEqual([t["title"] for t in history["chores"]], ["mow the lawn"])

    def test_the_agents_own_work_is_left_out_by_default(self):
        # "Who did what" is a question about people. The agent completing its
        # own scheduled task is not an answer to it.
        state.task_add(title="nightly audit")
        state.task_complete(task_id=1)          # no actor -> the agent
        state.task_add(title="mow the lawn")
        state.task_complete(task_id=2, actor="Nick")
        self.assertEqual([t["title"] for t in state.household_history()["chores"]],
                         ["mow the lawn"])
        self.assertEqual(len(state.household_history(include_agent=True)["chores"]), 2)

    def test_a_quiet_week_says_so_rather_than_returning_nothing(self):
        history = state.household_history()
        self.assertIn("Nothing recorded", history["summary"])
        self.assertEqual(history["chores"], [])

    def test_dropped_tasks_are_reported_separately_from_done_ones(self):
        state.task_add(title="call the dentist")
        state.task_drop(task_id=1, reason="they called us", actor="Sarah")
        history = state.household_history()
        self.assertEqual(history["chores"], [])
        self.assertEqual([t["title"] for t in history["dropped"]], ["call the dentist"])


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

    def test_the_agent_cannot_be_added_as_a_housemate(self):
        with self.assertRaises(state.ToolError) as caught:
            state.person_add(name="hermes")
        self.assertIn("agent's own name", str(caught.exception))


class TestIdentity(StateCase):
    """Who wrote this, when one bot serves two people."""

    SARAH = "389104857203441664"
    NATHAN = "201938475610293847"

    def test_an_env_var_default_person_is_ignored(self):
        # The bug this whole design exists for. STATE_PERSON is one value for
        # the whole process, so with a shared bot it credited every
        # unattributed write to whichever housemate configured the server -
        # silently, and wrongly half the time.
        self.assertEqual(state.LEGACY_PERSON, "Nathan")
        state.shopping_add(item="milk")
        added_by = state.q("SELECT added_by FROM shopping WHERE id=1")[0]["added_by"]
        self.assertEqual(added_by, "hermes")

    def test_an_unattributed_write_says_it_was_the_agent(self):
        result = state.task_add(title="restock salt")
        self.assertIn("No actor was given", result["notes"][0])
        self.assertEqual(state.q("SELECT created_by FROM tasks WHERE id=1")[0]["created_by"],
                         "hermes")

    def test_a_bare_discord_id_is_an_account_and_not_a_name(self):
        self.assertEqual(state.parse_identity(self.SARAH), ("discord", self.SARAH))
        self.assertEqual(state.parse_identity("telegram:12345"), ("telegram", "12345"))
        self.assertIsNone(state.parse_identity("Sarah"))
        # A house number is not a snowflake, and nobody is called 4.
        self.assertIsNone(state.parse_identity("42"))

    def test_an_unlinked_account_is_not_invented_as_a_person(self):
        result = state.shopping_add(item="oat milk", actor=self.SARAH)
        self.assertIn("No one is linked to", result["notes"][0])
        added_by = state.q("SELECT added_by FROM shopping WHERE id=1")[0]["added_by"]
        self.assertEqual(added_by, f"discord:{self.SARAH}")
        # Not a person called "389104857203441664".
        self.assertEqual([r["name"] for r in state.q("SELECT name FROM people")],
                         [f"discord:{self.SARAH}"])

    def test_linking_late_re_attributes_what_they_already_wrote(self):
        # The reason an unlinked account is allowed to write at all. Refusing
        # would make a new person's first message an error; guessing would put
        # it on someone else. Holding it under the raw id keeps it recoverable.
        state.shopping_add(item="oat milk", actor=self.SARAH)
        state.task_add(title="book the vet", actor=self.SARAH)
        result = state.person_link(name="Sarah", discord_id=self.SARAH)
        self.assertEqual(result["reattributed"], 2)
        self.assertEqual(state.q("SELECT added_by FROM shopping WHERE id=1")[0]["added_by"], "Sarah")
        self.assertEqual(state.q("SELECT created_by FROM tasks WHERE id=1")[0]["created_by"], "Sarah")
        # The placeholder is gone, not left beside the real person.
        self.assertEqual([r["name"] for r in state.q("SELECT name FROM people")], ["Sarah"])

    def test_two_people_on_one_bot_keep_their_own_records(self):
        state.person_link(name="Sarah", discord_id=self.SARAH)
        state.person_link(name="Nathan", discord_id=self.NATHAN)
        state.shopping_add(item="oat milk", actor=self.SARAH)
        state.shopping_add(item="coffee", actor=self.NATHAN)
        rows = {r["item"]: r["added_by"] for r in state.q("SELECT item, added_by FROM shopping")}
        self.assertEqual(rows, {"oat milk": "Sarah", "coffee": "Nathan"})

    def test_an_id_works_anywhere_a_name_does(self):
        state.person_link(name="Sarah", discord_id=self.SARAH)
        state.task_add(title="clean gutters", assignee=self.SARAH, actor=self.NATHAN)
        self.assertEqual(state.q("SELECT assignee FROM tasks WHERE id=1")[0]["assignee"], "Sarah")
        self.assertEqual(len(state.task_list(person=self.SARAH)["tasks"]), 1)

    def test_person_identify_says_what_to_do_about_a_stranger(self):
        unknown = state.person_identify(discord_id=self.SARAH)
        self.assertFalse(unknown["linked"])
        self.assertIn("person_link", unknown["summary"])
        state.person_link(name="Sarah", discord_id=self.SARAH)
        known = state.person_identify(discord_id=self.SARAH)
        self.assertTrue(known["linked"])
        self.assertEqual(known["person"], "Sarah")

    def test_a_handle_is_refused_as_an_account_id(self):
        with self.assertRaises(state.ToolError) as caught:
            state.person_link(name="Sarah", discord_id="sarah#1234")
        self.assertIn("not a Discord user id", str(caught.exception))

    def test_a_merge_moves_every_column_that_names_a_person(self):
        # Thirteen columns name a person. A merge that misses one leaves rows
        # pointing at a name no longer on the roster, and every individual
        # query still returns rows so nothing looks wrong.
        state.task_add(title="call the plumber", assignee="Sarha", actor="Sarha")
        state.task_complete(task_id=1, actor="Sarha")
        state.shopping_add(item="capers", actor="Sarha")
        state.pantry_set(item="rice", qty=2, actor="Sarha")
        state.meal_plan(date="today", dish="risotto", cook="Sarha", actor="Sarha")
        state.appointment_add(what="vet", date="tomorrow", who="Sarha", actor="Sarha")
        state.fact_record(subject="boiler", fact="serviced 2026-01", actor="Sarha")
        state.capture_add(raw="get bin bags", from_person="Sarha")
        state.journal_record(action="sent brief", actor="Sarha")

        state.person_merge(from_person="Sarha", into="Sarah")

        for table, column in state.ATTRIBUTION:
            left = state.q(f"SELECT COUNT(*) c FROM {table} WHERE {column}='Sarha'")[0]["c"]
            self.assertEqual(left, 0, f"{table}.{column} still says Sarha")
        self.assertNotIn("Sarha", [r["name"] for r in state.q("SELECT name FROM people")])

    def test_a_merge_takes_the_linked_account_with_it(self):
        # Otherwise the next message from that account recreates the row the
        # merge just deleted, and the correction silently undoes itself.
        state.shopping_add(item="oat milk", actor=self.SARAH)
        state.person_merge(from_person=f"discord:{self.SARAH}", into="Sarah")
        state.shopping_add(item="coffee", actor=self.SARAH)
        self.assertEqual(
            [r["added_by"] for r in state.q("SELECT added_by FROM shopping ORDER BY id")],
            ["Sarah", "Sarah"])

    def test_merging_someone_into_themselves_is_refused(self):
        state.person_add(name="Sarah", aliases="Sarah-Jane")
        with self.assertRaises(state.ToolError) as caught:
            state.person_merge(from_person="Sarah-Jane", into="Sarah")
        self.assertIn("already resolve to the same person", str(caught.exception))

    def test_relinking_an_account_reports_the_person_it_moved_from(self):
        state.person_link(name="Sarah", discord_id=self.SARAH)
        result = state.person_link(name="Nathan", discord_id=self.SARAH)
        self.assertIn("previously linked to Sarah", result["summary"])
        self.assertIn("person_merge", " ".join(result["notes"]))

    def test_a_read_never_registers_a_person(self):
        # The digest is the most-called tool here. If a mistyped name created a
        # housemate, the roster would fill up from the one place nobody looks.
        before = state.q("SELECT COUNT(*) c FROM people")[0]["c"]
        digest = state.household_digest(person="my wife")
        self.assertIn("Nobody here is called", digest["notes"][0])
        state.task_list(person="Wilhelmina")
        state.appointment_list(who="Wilhelmina")
        self.assertEqual(state.q("SELECT COUNT(*) c FROM people")[0]["c"], before)

    def test_status_flags_accounts_nobody_has_claimed(self):
        state.shopping_add(item="oat milk", actor=self.SARAH)
        status = state.state_status()
        self.assertEqual(status["unresolved"], [f"discord:{self.SARAH}"])
        self.assertIn("without being introduced", " ".join(status["warnings"]))

    def test_status_says_the_obsolete_env_var_is_ignored(self):
        self.assertIn("STATE_PERSON", " ".join(state.state_status()["warnings"]))


class TestMigration(StateCase):
    def test_a_database_from_before_identities_still_opens(self):
        # The household database is the one thing here that must survive every
        # upgrade. A new column on an existing table fails at the first write,
        # not at startup, which is the worst time to find out.
        import sqlite3

        path = os.path.join(TMP, "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.executescript(
            "CREATE TABLE people (name TEXT PRIMARY KEY, aliases TEXT DEFAULT '', "
            "created_at TEXT NOT NULL);"
            "INSERT INTO people VALUES ('Nathan', 'Nate', '2026-01-01 10:00:00');")
        legacy.commit()
        legacy.close()

        state._conn.close()  # setUp opened the empty one; point at the old file
        state._conn = None
        state.DB_PATH = path
        self.assertEqual(state.resolve_person("nate"), "Nathan")
        state.person_link(name="Nathan", discord_id="201938475610293847")
        self.assertEqual(state.person_identify(discord_id="201938475610293847")["person"],
                         "Nathan")


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
