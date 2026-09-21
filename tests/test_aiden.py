"""Day indexing, the safety gate, and a brewer that accepts a start and does nothing.

`aiden-mcp` has one irreversible write - starting a brew - and everything it can
get wrong is quiet:

  * a Sunday-first day array read as Monday-first, which schedules Tuesday's
    coffee for Wednesday and is discovered once a week, in the morning, badly;
  * an unknown brew state treated as "not brewing", because `None` is falsey and
    a gate written the obvious way starts a second brew during the first one;
  * a batch basket with no carafe under it, which is not a failed brew but a
    litre of hot coffee on the counter;
  * `/start` returning 200 and the machine never waking, which is the exact
    shape of FellowAiden-HomeAssistant issue #48 and the reason §3 read-back
    exists at all.

None of those raise. Each is a confident wrong answer.

The fake Fellow is deliberately able to be *deaf* - it accepts a start, returns
200, and leaves the device idle forever - because that is the failure you cannot
stage against a real brewer, and the one that matters most.
"""

import json
import os
import sys
import threading
import unittest
import http.server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402


BREWER = "dev123"

# A brewer that is idle, online, plugged in, lid shut, single basket in, on
# firmware new enough to start remotely. Every test starts from "ready" and
# breaks exactly one thing, so a blocker that fires is unambiguous.
READY = {
    "id": BREWER,
    "displayName": "Aiden",
    "firmwareVersion": "1.5.16",
    "isConnected": True,
    "state": None,
    "lidClosed": True,
    "missingWater": False,
    "cleaning": False,
    "rinsing": False,
    "singleBrewBasketPresent": True,
    "batchBrewBasketPresent": False,
    "carafePresent": False,
    # The Instant Brew preset: what a remote start would actually make.
    "ibSelectedProfileId": "p1",
    "ibWaterQuantity": 320,
    # The brew running or last run. Empty on an idle brewer, and the volume is
    # history, not intent. Deliberately set to a different number so any code
    # reading these as "what happens next" shows up as a wrong value rather
    # than a coincidentally right one.
    "brewingProfileId": None,
    "brewingWaterVolumeMl": 450,
}

PROFILES = [
    {"id": "p1", "title": "Morning Ethiopian", "ratio": 16, "overallTemperature": 96,
     "bloomEnabled": True, "bloomDuration": 30, "bloomRatio": 2, "bloomTemperature": 96,
     "ssPulsesEnabled": True, "ssPulsesNumber": 3, "ssPulsesInterval": 25,
     "instantBrew": True},
    {"id": "p2", "title": "Decaf Evening", "ratio": 17, "overallTemperature": 94,
     "bloomEnabled": False, "ssPulsesEnabled": True, "ssPulsesNumber": 2,
     "ssPulsesInterval": 30, "instantBrew": False},
]


class FakeFellow(http.server.BaseHTTPRequestHandler):
    """Just enough of Fellow's gateway to exercise the paths that matter."""

    protocol_version = "HTTP/1.1"

    @property
    def state(self):
        """Per-server, not per-class: tests overlap and a class attribute
        let one test's brewer answer another test's requests."""
        return self.server.fellow_state

    def log_message(self, *args):
        pass

    # -- plumbing ----------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _path(self):
        return self.path.split("?")[0]

    # -- routes ------------------------------------------------------------

    def do_POST(self):
        state = self.state
        path = self._path()
        if path == "/auth/login":
            state["logins"] += 1
            return self._send(200, {"accessToken": "at", "refreshToken": "rt"})
        if path == "/auth/refresh-token":
            return self._send(200, {"accessToken": "at2"})
        if path == f"/devices/{BREWER}/profiles":
            body = self._body()
            created = dict(body, id=f"p{len(state['profiles']) + 1}")
            state["profiles"].append(created)
            return self._send(200, created)
        if path == f"/devices/{BREWER}/schedules":
            body = self._body()
            if state["schedule_capped"]:
                return self._send(400, {
                    "message": "The maximum number of schedules (10) has been reached"
                })
            created = dict(body, id=f"s{len(state['schedules'])}")
            state["schedules"].append(created)
            return self._send(200, created)
        return self._send(404, {})

    def do_GET(self):
        state = self.state
        path = self._path()
        if path == "/devices":
            return self._send(200, [state["device"]])
        if path == f"/devices/{BREWER}":
            return self._send(200, state["device"])
        if path == f"/devices/{BREWER}/profiles":
            return self._send(200, state["profiles"])
        if path == f"/devices/{BREWER}/schedules":
            return self._send(200, state["schedules"])
        if path.startswith("/shared/"):
            return self._send(200, dict(state["shared"]))
        return self._send(404, {})

    def do_PATCH(self):
        state = self.state
        path = self._path()
        if path == f"/devices/{BREWER}/start":
            state["starts"] += 1
            if state["start_status"] >= 400:
                return self._send(state["start_status"], {"message": "nope"})
            # A responsive brewer starts. A deaf one takes the command and
            # stays idle, which is the case worth having.
            if state["start_wakes"]:
                state["device"]["state"] = {"value": "b"}
            return self._send(200, {"ibSelectedProfileId": "p1", "ibWaterQuantity": 320})
        if path == f"/devices/{BREWER}":
            body = self._body()
            if state["selection_sticks"]:
                state["device"].update(body)
            return self._send(state["patch_status"], {})
        if path.startswith(f"/devices/{BREWER}/schedules/"):
            sid = path.rsplit("/", 1)[-1]
            body = self._body()
            for index, schedule in enumerate(state["schedules"]):
                if schedule["id"] == sid:
                    state["schedules"][index] = dict(schedule, **body)
                    return self._send(200, state["schedules"][index])
            return self._send(404, {})
        return self._send(404, {})

    def do_DELETE(self):
        state = self.state
        path = self._path()
        for key in ("profiles", "schedules"):
            prefix = f"/devices/{BREWER}/{key}/"
            if path.startswith(prefix):
                target = path[len(prefix):]
                if state[f"keep_{key}"]:
                    return self._send(200, {})  # accepts, deletes nothing
                state[key] = [x for x in state[key] if x["id"] != target]
                return self._send(200, {})
        return self._send(404, {})


def serve(device=None, profiles=None, schedules=None, start_wakes=True,
          start_status=200, keep_profiles=False, keep_schedules=False, shared=None,
          selection_sticks=True, patch_status=200, schedule_capped=False):
    """Start a fake Fellow and return (module, state, stop)."""
    state = {
        "device": dict(device or READY),
        "profiles": [dict(p) for p in (profiles if profiles is not None else PROFILES)],
        "schedules": [dict(s) for s in (schedules or [])],
        "shared": shared or {},
        "start_wakes": start_wakes,
        "start_status": start_status,
        "selection_sticks": selection_sticks,
        "schedule_capped": schedule_capped,
        "patch_status": patch_status,
        "keep_profiles": keep_profiles,
        "keep_schedules": keep_schedules,
        "starts": 0,
        "logins": 0,
    }
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeFellow)
    httpd.fellow_state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    module = support.load("aiden_server", "aiden-mcp/aiden_mcp_server.py", {
        "FELLOW_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}",
        "FELLOW_EMAIL": "test@example.com",
        "FELLOW_PASSWORD": "secret",
        "FELLOW_CONFIRM_TIMEOUT": "4",
        "FELLOW_BREWER_NAME": None,
    })
    # Tests must not spend real seconds waiting for a fake brewer to wake.
    module.CONFIRM_INTERVAL = 0.05
    return module, state, httpd.shutdown


class DayAndTimeTest(unittest.TestCase):
    """Sunday-first. The whole file exists so this cannot drift."""

    @classmethod
    def setUpClass(cls):
        cls.server, _, cls.stop = serve()

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def test_weekdays_excludes_the_weekend_at_both_ends(self):
        # Index 0 is Sunday and index 6 is Saturday. A Monday-first reading
        # produces [T,T,T,T,T,F,F], which brews on Sunday and not on Friday.
        self.assertEqual(
            self.server.parse_days("weekdays"),
            [False, True, True, True, True, True, False],
        )

    def test_named_days_land_on_the_right_index(self):
        self.assertEqual(self.server.parse_days("sunday")[0], True)
        self.assertEqual(sum(self.server.parse_days("sunday")), 1)
        self.assertEqual(self.server.parse_days("monday")[1], True)
        self.assertEqual(self.server.parse_days("saturday")[6], True)

    def test_abbreviations_and_lists(self):
        flags = self.server.parse_days("mon,wed,fri")
        self.assertEqual(flags, [False, True, False, True, False, True, False])
        self.assertEqual(self.server.parse_days("Mon Wed Fri"), flags)
        self.assertEqual(self.server.parse_days("thurs")[4], True)

    def test_unknown_day_names_the_token(self):
        with self.assertRaises(self.server.ToolError) as caught:
            self.server.parse_days("mon,blursday")
        self.assertIn("blursday", str(caught.exception))

    def test_day_description_round_trips(self):
        for phrase in ("daily", "weekdays", "weekends"):
            self.assertEqual(
                self.server.describe_days(self.server.parse_days(phrase)), phrase
            )

    def test_time_parsing_is_24_hour(self):
        self.assertEqual(self.server.parse_time("06:45"), 6 * 3600 + 45 * 60)
        self.assertEqual(self.server.parse_time("18:45"), 18 * 3600 + 45 * 60)
        self.assertEqual(self.server.describe_time(24300), "06:45")

    def test_impossible_times_are_refused(self):
        for bad in ("25:00", "06:70", "6pm", "645"):
            with self.assertRaises(self.server.ToolError):
                self.server.parse_time(bad)


class SafetyGateTest(unittest.TestCase):
    """Each blocker on its own, from a brewer that is otherwise ready."""

    @classmethod
    def setUpClass(cls):
        cls.server, _, cls.stop = serve()

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def blockers(self, **changes):
        return self.server.start_blockers(dict(READY, **changes))

    def test_a_ready_brewer_has_no_blockers(self):
        self.assertEqual(self.blockers(), [])

    def test_each_physical_condition_blocks_and_says_which(self):
        cases = [
            ({"lidClosed": False}, "lid"),
            ({"missingWater": True}, "reservoir"),
            ({"cleaning": True}, "cleaning"),
            ({"rinsing": True}, "rinsing"),
            ({"isConnected": False}, "offline"),
            ({"state": {"value": "b"}}, "already running"),
            ({"firmwareVersion": "1.5.15"}, "Firmware"),
            ({"singleBrewBasketPresent": False}, "basket"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                found = self.blockers(**changes)
                self.assertTrue(found, f"{changes} should block")
                self.assertIn(expected.lower(), " ".join(found).lower())

    def test_batch_basket_without_a_carafe_blocks(self):
        # Not a failed brew - a litre of coffee on the counter.
        found = self.blockers(
            singleBrewBasketPresent=False, batchBrewBasketPresent=True, carafePresent=False
        )
        self.assertTrue(any("carafe" in b.lower() for b in found))

    def test_single_basket_does_not_need_a_carafe(self):
        # The mug sits directly under it, so demanding a carafe here would
        # block every single-cup brew for a reason that does not exist.
        self.assertEqual(
            self.blockers(singleBrewBasketPresent=True, carafePresent=False), []
        )

    def test_unknown_brew_state_is_none_not_false(self):
        # `is_brewing` returning False for "the payload does not say" is how a
        # second brew gets started during the first.
        self.assertIsNone(self.server.is_brewing({}))
        self.assertIs(self.server.is_brewing({"state": None}), False)
        self.assertIs(self.server.is_brewing({"state": {"value": "b"}}), True)

    def test_either_water_indicator_wins(self):
        self.assertIs(self.server.is_missing_water({"missingWater": False,
                                                    "state": {"missing_water": True}}), True)
        self.assertIs(self.server.is_missing_water({"missingWater": True, "state": None}), True)

    def test_firmware_below_the_floor_is_refused(self):
        self.assertFalse(self.server.supports_remote_start({"firmwareVersion": "1.5.15"}))
        self.assertTrue(self.server.supports_remote_start({"firmwareVersion": "1.5.16"}))
        self.assertTrue(self.server.supports_remote_start({"firmwareVersion": "v1.6.0"}))
        self.assertFalse(self.server.supports_remote_start({}))

    def test_brew_phase_names_pulses(self):
        self.assertEqual(self.server.brew_phase({"state": {"value": "p3"}}), "pulse_3")
        self.assertEqual(self.server.brew_phase({"state": {"value": "b"}}), "bloom")
        self.assertEqual(self.server.brew_phase({"state": None}), "idle")


class BrewNowTest(unittest.TestCase):
    """The one irreversible write."""

    def test_a_confirmed_brew_reports_the_phase(self):
        server, state, stop = serve(start_wakes=True)
        try:
            result = server.brew_now()
            self.assertTrue(result["ok"])
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["recipe"], "Morning Ethiopian")
            self.assertEqual(result["phase"], "bloom")
            self.assertEqual(state["starts"], 1)
        finally:
            stop()

    def test_a_deaf_brewer_is_reported_unconfirmed_not_successful(self):
        # Fellow returns 200, the machine never wakes. Reporting this as
        # "brewing" is how someone comes downstairs to an empty carafe.
        server, state, stop = serve(start_wakes=False)
        try:
            result = server.brew_now(confirm_seconds=1)
            self.assertFalse(result["confirmed"])
            self.assertIn("has not reported brewing", result["summary"])
            self.assertEqual(state["starts"], 1)
        finally:
            stop()

    def test_a_blocked_brewer_is_never_dispatched_to(self):
        server, state, stop = serve(device=dict(READY, lidClosed=False))
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now()
            self.assertIn("lid", str(caught.exception).lower())
            self.assertEqual(state["starts"], 0)
        finally:
            stop()

    def test_naming_a_recipe_selects_it_then_brews_it(self):
        server, state, stop = serve()
        try:
            result = server.brew_now(profile="Decaf Evening", confirm_seconds=2)
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["recipe"], "Decaf Evening")
            self.assertEqual(state["device"]["ibSelectedProfileId"], "p2")
            self.assertEqual(state["starts"], 1)
        finally:
            stop()

    def test_a_selection_that_does_not_take_stops_the_brew(self):
        # Brewing the wrong recipe under the right name is worse than brewing
        # nothing, so a failed select must not fall through to the start.
        server, state, stop = serve(selection_sticks=False)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now(profile="Decaf Evening")
            message = str(caught.exception)
            self.assertIn("Could not select", message)
            self.assertIn("nothing was brewed", message)
            self.assertEqual(state["starts"], 0)
        finally:
            stop()

    def test_the_already_selected_recipe_brews_without_a_write(self):
        server, state, stop = serve(selection_sticks=False)
        try:
            result = server.brew_now(profile="Morning Ethiopian", confirm_seconds=2)
            self.assertTrue(result["confirmed"])
            self.assertEqual(state["starts"], 1)
        finally:
            stop()

    def test_water_without_a_recipe_is_refused(self):
        server, state, stop = serve()
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now(water_ml=450)
            self.assertIn("set_instant_brew", str(caught.exception))
            self.assertEqual(state["starts"], 0)
        finally:
            stop()

    def test_a_server_error_on_start_is_not_retried(self):
        # A retried start that the first attempt actually began is a second pot,
        # and there is no remote stop to undo it with.
        server, state, stop = serve(start_status=503)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now()
            self.assertIn("Do NOT retry", str(caught.exception))
            self.assertEqual(state["starts"], 1)
        finally:
            stop()

    def test_an_unknown_recipe_lists_the_real_ones(self):
        server, _, stop = serve()
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now(profile="Nonexistent")
            self.assertIn("Morning Ethiopian", str(caught.exception))
        finally:
            stop()


class StatusTest(unittest.TestCase):
    def test_status_names_what_would_be_brewed(self):
        server, _, stop = serve()
        try:
            result = server.brew_status()
            self.assertTrue(result["ready_to_brew"])
            self.assertEqual(result["instant_brew_recipe"], "Morning Ethiopian")
            self.assertEqual(result["instant_brew_water_ml"], 320)
            self.assertIn("Morning Ethiopian", result["summary"])
        finally:
            stop()

    def test_status_leads_with_the_blocker(self):
        server, _, stop = serve(device=dict(READY, missingWater=True))
        try:
            result = server.brew_status()
            self.assertFalse(result["ready_to_brew"])
            self.assertIn("reservoir", result["summary"].lower())
        finally:
            stop()


class ProfileTest(unittest.TestCase):
    def test_creating_a_profile_reads_it_back(self):
        server, state, stop = serve()
        try:
            result = server.create_profile(title="Test Blend", ratio=16, temperature_c=95)
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["profile"]["title"], "Test Blend")
            saved = [p for p in state["profiles"] if p["title"] == "Test Blend"][0]
            self.assertEqual(saved["overallTemperature"], 95)
            # Pulse temperatures must match the pulse count or Fellow 400s.
            self.assertEqual(len(saved["ssPulseTemperatures"]), saved["ssPulsesNumber"])
        finally:
            stop()

    def test_off_grid_values_snap_and_far_ones_are_refused(self):
        server, _, stop = serve()
        try:
            self.assertEqual(server.nearest_step(94.3, server.TEMP_STEPS, "t"), 94.5)
            with self.assertRaises(server.ToolError):
                server.nearest_step(25, server.RATIO_STEPS, "ratio")
        finally:
            stop()

    def test_an_apostrophe_is_caught_before_it_ships(self):
        server, _, stop = serve()
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.create_profile(title="Nick's Morning")
            self.assertIn("apostrophe", str(caught.exception))
        finally:
            stop()

    def test_a_duplicate_title_is_refused(self):
        server, _, stop = serve()
        try:
            with self.assertRaises(server.ToolError):
                server.create_profile(title="Morning Ethiopian")
        finally:
            stop()

    def test_deleting_a_scheduled_profile_is_refused(self):
        # Deleting it would leave the schedule pointing at nothing, and the
        # brew silently stops happening.
        schedules = [{"id": "s0", "profileId": "p1", "days": [True] * 7,
                      "secondFromStartOfTheDay": 24300, "enabled": True,
                      "amountOfWater": 950}]
        server, state, stop = serve(schedules=schedules)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.delete_profile(title="Morning Ethiopian")
            self.assertIn("cancel_schedule", str(caught.exception))
            self.assertEqual(len(state["profiles"]), 2)
        finally:
            stop()

    def test_a_delete_that_did_not_take_is_reported_as_failure(self):
        server, _, stop = serve(keep_profiles=True)
        try:
            result = server.delete_profile(title="Decaf Evening")
            self.assertFalse(result["ok"])
            self.assertIn("still on", result["error"])
        finally:
            stop()

    def test_an_ambiguous_partial_title_is_refused_not_guessed(self):
        profiles = [dict(PROFILES[0], title="Morning A", id="p1"),
                    dict(PROFILES[1], title="Morning B", id="p2")]
        server, _, stop = serve(profiles=profiles)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.resolve_profile("morning")
            self.assertIn("more than one", str(caught.exception))
        finally:
            stop()


class ScheduleTest(unittest.TestCase):
    def test_scheduling_reads_back_and_reports_in_human_terms(self):
        server, state, stop = serve()
        try:
            result = server.schedule_brew(
                time="06:45", days="weekdays", profile="Morning Ethiopian", water_ml=950
            )
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["schedule"]["time"], "06:45")
            self.assertEqual(result["schedule"]["days"], "weekdays")
            self.assertEqual(result["schedule"]["recipe"], "Morning Ethiopian")
            saved = state["schedules"][0]
            self.assertEqual(saved["secondFromStartOfTheDay"], 24300)
            self.assertEqual(saved["days"], [False, True, True, True, True, True, False])
            self.assertEqual(saved["profileId"], "p1")
        finally:
            stop()

    def test_water_outside_fellows_range_is_refused_locally(self):
        server, state, stop = serve()
        try:
            for bad in (100, 2000):
                with self.assertRaises(server.ToolError):
                    server.schedule_brew(time="07:00", days="daily",
                                         profile="Morning Ethiopian", water_ml=bad)
            self.assertEqual(state["schedules"], [])
        finally:
            stop()

    def test_a_duplicate_schedule_is_refused(self):
        server, _, stop = serve()
        try:
            server.schedule_brew(time="06:45", days="weekdays", profile="Morning Ethiopian")
            with self.assertRaises(server.ToolError) as caught:
                server.schedule_brew(time="06:45", days="daily", profile="Morning Ethiopian")
            self.assertIn("already brews", str(caught.exception))
        finally:
            stop()

    def test_two_schedules_at_one_time_ask_which_rather_than_picking(self):
        schedules = [
            {"id": "s0", "profileId": "p1", "days": [True] * 7,
             "secondFromStartOfTheDay": 24300, "enabled": True, "amountOfWater": 950},
            {"id": "s1", "profileId": "p2", "days": [True] * 7,
             "secondFromStartOfTheDay": 24300, "enabled": True, "amountOfWater": 320},
        ]
        server, _, stop = serve(schedules=schedules)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.cancel_schedule(time="06:45")
            self.assertIn("Pass profile=", str(caught.exception))
            # Named, it resolves.
            result = server.cancel_schedule(time="06:45", profile="Decaf Evening")
            self.assertTrue(result["ok"])
        finally:
            stop()

    def test_toggling_reads_the_flag_back(self):
        schedules = [{"id": "s0", "profileId": "p1", "days": [True] * 7,
                      "secondFromStartOfTheDay": 24300, "enabled": True,
                      "amountOfWater": 950}]
        server, state, stop = serve(schedules=schedules)
        try:
            result = server.set_schedule_enabled(time="06:45", enabled=False)
            self.assertTrue(result["confirmed"])
            self.assertIs(state["schedules"][0]["enabled"], False)
            # Already-off is a no-op, not a second write.
            again = server.set_schedule_enabled(time="06:45", enabled=False)
            self.assertIn("already disabled", again["summary"])
        finally:
            stop()

    def test_cancelling_a_missing_schedule_lists_the_real_ones(self):
        schedules = [{"id": "s0", "profileId": "p1", "days": [True] * 7,
                      "secondFromStartOfTheDay": 24300, "enabled": True,
                      "amountOfWater": 950}]
        server, _, stop = serve(schedules=schedules)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.cancel_schedule(time="09:00")
            self.assertIn("06:45", str(caught.exception))
        finally:
            stop()


class AuthTest(unittest.TestCase):
    def test_no_credentials_is_a_sentence_not_a_traceback(self):
        server, _, stop = serve()
        try:
            server._session["access"] = None
            original = server.FELLOW_EMAIL
            server.FELLOW_EMAIL = ""
            try:
                with self.assertRaises(server.ToolError) as caught:
                    server._login()
                self.assertIn("FELLOW_EMAIL", str(caught.exception))
            finally:
                server.FELLOW_EMAIL = original
        finally:
            stop()

    def test_tools_are_registered_with_mcpkit(self):
        server, _, stop = serve()
        try:
            import mcpkit
            for name in ("brew_now", "brew_status", "schedule_brew", "cancel_schedule"):
                self.assertIn(name, mcpkit.TOOLS)
        finally:
            stop()



class InstantBrewFieldTest(unittest.TestCase):
    """Which field answers "what would a start make?" - the one real device
    disagreed with the first implementation, and silently."""

    def test_status_reports_the_preset_not_the_last_brew(self):
        server, _, stop = serve()
        try:
            result = server.brew_status()
            # ibSelectedProfileId / ibWaterQuantity, not brewingWaterVolumeMl.
            self.assertEqual(result["instant_brew_recipe"], "Morning Ethiopian")
            self.assertEqual(result["instant_brew_water_ml"], 320)
            self.assertNotEqual(result["instant_brew_water_ml"], 450)
            # The last brew is still reported, just not as a forecast.
            self.assertEqual(result["current_or_last_brew_water_ml"], 450)
        finally:
            stop()

    def test_an_idle_brewer_still_knows_what_it_would_make(self):
        # brewingProfileId is None here, as it is on a real idle Aiden. Reading
        # that field left the answer null on a brewer that knew perfectly well.
        server, _, stop = serve(device=dict(READY, brewingProfileId=None))
        try:
            self.assertEqual(
                server.brew_status()["instant_brew_recipe"], "Morning Ethiopian"
            )
        finally:
            stop()

    def test_no_preset_selected_is_said_plainly_not_papered_over(self):
        server, _, stop = serve(device=dict(READY, ibSelectedProfileId=None))
        try:
            result = server.brew_status()
            self.assertIsNone(result["instant_brew_recipe"])
            self.assertIn("no Instant Brew preset", result["summary"])
        finally:
            stop()

    def test_no_preset_and_no_recipe_named_refuses_to_brew(self):
        # Refusing beats brewing something that cannot be named afterwards.
        server, state, stop = serve(device=dict(READY, ibSelectedProfileId=None))
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.brew_now()
            self.assertIn("not reporting an Instant Brew preset", str(caught.exception))
            self.assertEqual(state["starts"], 0)
        finally:
            stop()

    def test_naming_a_recipe_rescues_a_brewer_with_no_preset(self):
        server, state, stop = serve(device=dict(READY, ibSelectedProfileId=None))
        try:
            result = server.brew_now(profile="Morning Ethiopian", confirm_seconds=2)
            self.assertTrue(result["confirmed"])
            self.assertEqual(state["starts"], 1)
        finally:
            stop()


class InstantBrewSelectionTest(unittest.TestCase):
    """Writing the Instant Brew preset through the generic device PATCH.

    Fellow rejects its own client's dedicated selected-profile route, so this
    door may be shut too. The point of these tests is that a shut door is
    reported as shut rather than as success.
    """

    def test_selecting_a_recipe_reads_back(self):
        server, state, stop = serve()
        try:
            result = server.set_instant_brew(profile="Decaf Evening", water_ml=450,
                                             settle_seconds=1)
            self.assertTrue(result["ok"])
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["recipe"], "Decaf Evening")
            self.assertEqual(result["water_ml"], 450)
            self.assertEqual(result["previous_recipe"], "Morning Ethiopian")
            self.assertEqual(state["device"]["ibSelectedProfileId"], "p2")
            self.assertEqual(state["device"]["ibWaterQuantity"], 450)
        finally:
            stop()

    def test_a_patch_that_changes_nothing_is_a_failure_not_a_success(self):
        # The case worth having: Fellow answers 200 and the preset never moves.
        server, _, stop = serve(selection_sticks=False)
        try:
            result = server.set_instant_brew(profile="Decaf Evening", settle_seconds=1)
            self.assertFalse(result["ok"])
            self.assertIn("still has", result["error"])
            self.assertIn("Morning Ethiopian", result["error"])
            self.assertIn("schedule_brew", result["error"])
            self.assertEqual(result["instant_brew_recipe"], "Morning Ethiopian")
        finally:
            stop()

    def test_a_refused_patch_names_the_surviving_preset(self):
        server, _, stop = serve(patch_status=403, selection_sticks=False)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.set_instant_brew(profile="Decaf Evening", settle_seconds=1)
            message = str(caught.exception)
            self.assertIn("403", message)
            self.assertIn("Morning Ethiopian", message)
        finally:
            stop()

    def test_selecting_what_is_already_selected_writes_nothing(self):
        server, state, stop = serve(selection_sticks=False)
        try:
            result = server.set_instant_brew(profile="Morning Ethiopian", settle_seconds=1)
            self.assertTrue(result["ok"])
            self.assertIn("already set", result["summary"])
            self.assertEqual(state["device"]["ibSelectedProfileId"], "p1")
        finally:
            stop()

    def test_water_outside_range_is_refused_before_any_write(self):
        server, state, stop = serve()
        try:
            with self.assertRaises(server.ToolError):
                server.set_instant_brew(profile="Decaf Evening", water_ml=5000)
            self.assertEqual(state["device"]["ibSelectedProfileId"], "p1")
        finally:
            stop()

    def test_an_unknown_recipe_lists_the_real_ones(self):
        server, _, stop = serve()
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.set_instant_brew(profile="Nonexistent")
            self.assertIn("Morning Ethiopian", str(caught.exception))
        finally:
            stop()


class ScheduleLimitTest(unittest.TestCase):
    """Fellow caps schedules at ten and answers the eleventh with a 400."""

    def test_the_cap_names_what_is_already_there(self):
        schedules = [
            {"id": "s%d" % n, "profileId": "p1", "days": [True] * 7,
             "secondFromStartOfTheDay": 3600 * n, "enabled": n % 2 == 0,
             "amountOfWater": 950}
            for n in range(10)
        ]
        server, _, stop = serve(schedules=schedules, schedule_capped=True)
        try:
            with self.assertRaises(server.ToolError) as caught:
                server.schedule_brew(time="16:44", days="weekdays",
                                     profile="Morning Ethiopian")
            message = str(caught.exception)
            self.assertIn("maximum of 10", message)
            self.assertIn("cancel_schedule", message)
            # It must say what occupies the slots, or there is no way to decide
            # which one to drop.
            self.assertIn("Morning Ethiopian", message)
            self.assertIn("[disabled]", message)
        finally:
            stop()


class ProfileSummaryTest(unittest.TestCase):
    """Two fields a real brewer reported differently than assumed."""

    def test_instant_brew_flag_comes_from_the_device_not_the_profile(self):
        # Every real profile carries instantBrew False, including the selected
        # one, so trusting that field marks nothing as loaded.
        profiles = [dict(p, instantBrew=False) for p in PROFILES]
        server, _, stop = serve(profiles=profiles)
        try:
            rows = server.list_profiles()["profiles"]
            flagged = [r["title"] for r in rows if r["is_instant_brew_recipe"]]
            self.assertEqual(flagged, ["Morning Ethiopian"])
        finally:
            stop()

    def test_temperature_falls_back_to_the_pulse_temperatures(self):
        # Plenty of real profiles omit overallTemperature entirely. Reporting
        # null for a recipe that plainly brews at some temperature is a gap the
        # caller cannot act on.
        profiles = [
            dict(PROFILES[0], id="p1", title="No Overall",
                 overallTemperature=None, ssPulseTemperatures=[97.0, 97.0]),
        ]
        server, _, stop = serve(profiles=profiles)
        try:
            row = server.list_profiles()["profiles"][0]
            self.assertEqual(row["temperature_c"], 97.0)
            # An inferred number must not look like a stated one.
            self.assertEqual(row["temperature_source"], "pulses")
        finally:
            stop()

    def test_a_stated_temperature_is_marked_as_stated(self):
        server, _, stop = serve()
        try:
            row = server.list_profiles()["profiles"][0]
            self.assertEqual(row["temperature_c"], 96)
            self.assertEqual(row["temperature_source"], "profile")
        finally:
            stop()

    def test_a_pulseless_profile_reports_no_temperature_rather_than_the_bloom(self):
        # A cold brew carries bloom fields the machine ignores. Falling back to
        # them described a cold brew as brewing at 99C - a confident wrong
        # number in place of an honest gap.
        profiles = [
            dict(PROFILES[0], id="p1", title="Cold Brew", overallTemperature=None,
                 ssPulsesEnabled=False, ssPulseTemperatures=[],
                 batchPulsesEnabled=False, batchPulseTemperatures=[],
                 bloomEnabled=True, bloomTemperature=99.0),
        ]
        server, _, stop = serve(profiles=profiles)
        try:
            row = server.list_profiles()["profiles"][0]
            self.assertIsNone(row["temperature_c"])
            self.assertIsNone(row["temperature_source"])
            # The bloom block still reports its own value; it is just not
            # promoted into the brew temperature.
            self.assertEqual(row["bloom"]["temperature_c"], 99.0)
        finally:
            stop()


class OfflineTest(unittest.TestCase):
    """An offline brewer's readings are last-known, not current."""

    def test_offline_leads_with_offline_and_flags_the_readings(self):
        # The cloud keeps serving the last snapshot, so a brewer unplugged
        # yesterday still reports a closed lid and a full reservoir.
        server, _, stop = serve(device=dict(READY, isConnected=False))
        try:
            result = server.brew_status()
            self.assertFalse(result["online"])
            self.assertTrue(result["readings_stale"])
            self.assertFalse(result["ready_to_brew"])
            self.assertIn("offline", result["summary"].lower())
            self.assertIn("stale", result["summary"].lower())
        finally:
            stop()

    def test_an_online_brewer_is_not_flagged_stale(self):
        server, _, stop = serve()
        try:
            self.assertFalse(server.brew_status()["readings_stale"])
        finally:
            stop()

if __name__ == "__main__":
    unittest.main()
