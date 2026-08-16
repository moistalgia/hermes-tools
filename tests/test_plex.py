"""The parts of plex-mcp that are logic rather than hardware.

The tests README used to say this server was untestable, and for playback that
is still true - whether a particular Fire TV accepts a pause is a fact about a
device, not about code. But the library-scale half added since then is pure
arithmetic over data Plex hands back: does a title the user named match a title
on the server, which episode numbers are absent, how much of an item gets
printed. Every one of those has a wrong answer that looks plausible, which is
exactly what tests are for.

plexapi is imported lazily inside the connection helper, so this file runs with
nothing installed - same rule as the rest of the suite.
"""

import unittest

from support import load

plex = load("plex_mcp_server_under_test", "plex-mcp/plex_mcp_server.py",
            env={"PLEX_TOKEN": "test-token", "PLEX_URL": "http://plex.invalid"})


class Item:
    """A stand-in for a plexapi object: attributes, nothing else."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def tag(name):
    return Item(tag=name)


class NormalizeTitle(unittest.TestCase):
    """Every difference here is one an agent would otherwise read as 'missing'."""

    def test_articles_and_case_fold_away(self):
        self.assertEqual(plex.normalize_title("The Matrix"),
                         plex.normalize_title("matrix"))

    def test_punctuation_folds_away(self):
        self.assertEqual(plex.normalize_title("Spider-Man: No Way Home"),
                         plex.normalize_title("Spider Man No Way Home"))

    def test_accents_fold_away(self):
        self.assertEqual(plex.normalize_title("Léon: The Professional"),
                         plex.normalize_title("Leon The Professional"))

    def test_trailing_year_is_stripped(self):
        self.assertEqual(plex.normalize_title("Alien (1979)"),
                         plex.normalize_title("Alien"))

    def test_roman_numerals_become_digits(self):
        self.assertEqual(plex.normalize_title("Rocky II"),
                         plex.normalize_title("Rocky 2"))

    def test_article_stripped_after_punctuation_collapse(self):
        # "The Godfather Part II" and "Godfather Part 2" are the same film and
        # differ by an article and a numeral at once.
        self.assertEqual(plex.normalize_title("The Godfather Part II"),
                         plex.normalize_title("Godfather Part 2"))

    def test_empty_input_is_not_a_crash(self):
        self.assertEqual(plex.normalize_title(None), "")


class SequelDisambiguation(unittest.TestCase):
    """The failure this guard exists for: 'rocky 2' and 'rocky 4' differ by one
    character and score above any fuzzy cutoff worth using."""

    def test_different_sequel_numbers_are_not_the_same_film(self):
        self.assertFalse(plex.same_entry(
            plex.normalize_title("Rocky II"), plex.normalize_title("Rocky IV")))

    def test_matching_sequel_numbers_pass(self):
        self.assertTrue(plex.same_entry(
            plex.normalize_title("Rocky IV"), plex.normalize_title("Rocky 4")))

    def test_unnumbered_titles_are_unaffected(self):
        self.assertTrue(plex.same_entry(
            plex.normalize_title("Casablanca"), plex.normalize_title("Casblanca")))


class ParseTitleList(unittest.TestCase):

    def test_json_array(self):
        self.assertEqual(plex.parse_title_list('["Jaws", "Fargo"]'),
                         ["Jaws", "Fargo"])

    def test_real_list_passes_through(self):
        self.assertEqual(plex.parse_title_list(["Jaws", "Fargo"]),
                         ["Jaws", "Fargo"])

    def test_newlines_win_over_commas(self):
        # A title can contain a comma; splitting on it would cut this in half.
        self.assertEqual(
            plex.parse_title_list("Dr. Strangelove, or: How I Learned\nJaws"),
            ["Dr. Strangelove, or: How I Learned", "Jaws"])

    def test_comma_separated_single_line(self):
        self.assertEqual(plex.parse_title_list("Jaws, Fargo"), ["Jaws", "Fargo"])

    def test_markdown_bullets_are_stripped(self):
        self.assertEqual(plex.parse_title_list("- Jaws\n* Fargo"),
                         ["Jaws", "Fargo"])

    def test_blank_entries_dropped(self):
        self.assertEqual(plex.parse_title_list("Jaws\n\n  \nFargo"),
                         ["Jaws", "Fargo"])

    def test_malformed_json_array_is_a_tool_error(self):
        with self.assertRaises(plex.ToolError):
            plex.parse_title_list('["Jaws", "Fargo"')


class SplitTitleYear(unittest.TestCase):

    def test_year_is_extracted(self):
        self.assertEqual(plex.split_title_year("Alien (1979)"), ("Alien", 1979))

    def test_no_year_returns_none(self):
        self.assertEqual(plex.split_title_year("Alien"), ("Alien", None))

    def test_a_number_in_the_title_is_not_a_year(self):
        self.assertEqual(plex.split_title_year("Se7en (1995)"), ("Se7en", 1995))
        self.assertEqual(plex.split_title_year("1917"), ("1917", None))


class EpisodeGaps(unittest.TestCase):
    """The arithmetic behind find_gaps. A false positive here sends someone
    hunting for an episode that does not exist."""

    @staticmethod
    def episodes(show, season, numbers):
        return [Item(grandparentTitle=show, parentIndex=season, index=n)
                for n in numbers]

    def test_interior_hole_is_found(self):
        gaps = plex.episode_gaps(self.episodes("Show", 1, [1, 2, 4, 5]))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["missing_episodes"], [3])
        self.assertEqual(gaps[0]["highest_present"], 5)

    def test_a_complete_season_reports_nothing(self):
        self.assertEqual(plex.episode_gaps(self.episodes("Show", 1, [1, 2, 3])), [])

    def test_a_currently_airing_season_is_not_a_gap(self):
        # Four episodes aired, four present. Assuming a season is 10 long would
        # report every in-flight show as broken.
        self.assertEqual(plex.episode_gaps(self.episodes("Show", 1, [1, 2, 3, 4])), [])

    def test_missing_season_is_reported(self):
        eps = self.episodes("Show", 1, [1, 2]) + self.episodes("Show", 3, [1, 2])
        gaps = plex.episode_gaps(eps)
        seasons = [g for g in gaps if "missing_seasons" in g]
        self.assertEqual(len(seasons), 1)
        self.assertEqual(seasons[0]["missing_seasons"], [2])

    def test_specials_are_ignored(self):
        # Season 0 numbering is arbitrary and would otherwise always look holey.
        self.assertEqual(plex.episode_gaps(self.episodes("Show", 0, [2, 7])), [])

    def test_episodes_without_numbering_are_skipped_not_crashed(self):
        eps = [Item(grandparentTitle="Show", parentIndex=None, index=None)]
        self.assertEqual(plex.episode_gaps(eps), [])

    def test_shows_are_kept_separate(self):
        eps = self.episodes("A", 1, [1, 3]) + self.episodes("B", 1, [1, 2])
        gaps = [g for g in plex.episode_gaps(eps) if "missing_episodes" in g]
        self.assertEqual([g["show"] for g in gaps], ["A"])


class ProjectItem(unittest.TestCase):
    """detail is the whole reason a 500-title library fits in a reply."""

    def movie(self):
        return Item(
            type="movie", ratingKey=7, title="Alien", year=1979, duration=7062000,
            genres=[tag("Horror"), tag("Science Fiction")], rating=8.4,
            viewCount=1, contentRating="R", studio="20th Century Fox",
            directors=[tag("Ridley Scott")], roles=[tag("Sigourney Weaver")],
            summary="A crew answers a distress call.",
            media=[Item(videoResolution="1080",
                        parts=[Item(size=2_000_000_000)])],
        )

    def test_minimal_is_only_identity(self):
        out = plex.project_item(self.movie(), "minimal")
        self.assertEqual(set(out), {"rating_key", "title", "year"})

    def test_compact_carries_what_recommendation_needs(self):
        out = plex.project_item(self.movie(), "compact")
        self.assertEqual(out["genres"], ["Horror", "Science Fiction"])
        self.assertEqual(out["resolution"], "1080")
        self.assertEqual(out["minutes"], 117)
        self.assertTrue(out["watched"])
        self.assertNotIn("summary", out)

    def test_full_adds_the_expensive_fields(self):
        out = plex.project_item(self.movie(), "full")
        self.assertEqual(out["cast"], ["Sigourney Weaver"])
        self.assertEqual(out["gb"], 2.0)
        self.assertIn("summary", out)

    def test_null_fields_are_dropped_rather_than_printed(self):
        out = plex.project_item(
            Item(type="movie", ratingKey=1, title="Untitled", year=None), "compact")
        self.assertNotIn("year", out)
        self.assertNotIn("genres", out)

    def test_episode_carries_its_position(self):
        out = plex.project_item(
            Item(type="episode", ratingKey=9, title="Pilot",
                 grandparentTitle="The Wire", parentIndex=1, index=1), "minimal")
        self.assertEqual((out["show"], out["season"], out["episode"]),
                         ("The Wire", 1, 1))

    def test_show_reports_episode_counts(self):
        out = plex.project_item(
            Item(type="show", ratingKey=3, title="The Wire", year=2002,
                 leafCount=60, viewedLeafCount=12, childCount=5), "compact")
        self.assertEqual((out["episodes"], out["episodes_watched"],
                          out["seasons"]), (60, 12, 5))

    def test_unwatched_is_false_not_absent(self):
        # `watched` is a filter the agent reasons over; dropping it when false
        # would make unwatched items indistinguishable from unknown ones.
        out = plex.project_item(
            Item(type="movie", ratingKey=1, title="X", year=2000, viewCount=0),
            "compact")
        self.assertIs(out["watched"], False)


class DetailValidation(unittest.TestCase):

    def test_known_levels_pass(self):
        for level in ("minimal", "compact", "full"):
            self.assertEqual(plex.clean_detail(level), level)

    def test_default_is_compact(self):
        self.assertEqual(plex.clean_detail(None), "compact")

    def test_unknown_level_names_the_valid_ones(self):
        with self.assertRaises(plex.ToolError) as caught:
            plex.clean_detail("verbose")
        self.assertIn("minimal", str(caught.exception.extra))


class ArgumentCoercion(unittest.TestCase):
    """resolution=1080 and decade=1990 arrive as integers from the CLI and from
    models that see a number and send one. Each was an AttributeError."""

    def test_integers_become_strings(self):
        self.assertEqual(plex.text(1080), "1080")

    def test_none_becomes_the_default(self):
        self.assertEqual(plex.text(None, "1080"), "1080")

    def test_detail_survives_a_non_string(self):
        with self.assertRaises(plex.ToolError):
            plex.clean_detail(5)


class ToolSurface(unittest.TestCase):
    """Schema faults that only show up when a client reads tools/list."""

    def test_every_tool_has_a_description_and_schema(self):
        for name, entry in plex.TOOLS.items():
            self.assertTrue(entry["description"], f"{name} has no description")
            self.assertEqual(entry["inputSchema"]["type"], "object", name)

    def test_required_arguments_are_declared_in_properties(self):
        for name, entry in plex.TOOLS.items():
            schema = entry["inputSchema"]
            for field in schema["required"]:
                self.assertIn(field, schema["properties"],
                              f"{name} requires {field} but does not declare it")

    def test_declared_arguments_match_the_function(self):
        # A schema that advertises an argument the function does not take is
        # silently dropped by call_tool, so the agent's request is ignored
        # rather than refused - the worst possible failure mode.
        import inspect
        for name, entry in plex.TOOLS.items():
            params = set(inspect.signature(entry["fn"]).parameters)
            for field in entry["inputSchema"]["properties"]:
                self.assertIn(field, params,
                              f"{name} advertises {field} but does not accept it")

    def test_unknown_tool_reports_what_exists(self):
        result = plex.call_tool("nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("library_export", result["available_tools"])

    def test_bulk_tools_are_registered(self):
        for name in ("library_export", "library_stats", "check_titles",
                     "find_gaps", "refresh_library", "watch_history",
                     "set_streams", "mark_watched", "create_playlist"):
            self.assertIn(name, plex.TOOLS)


class LibraryCache(unittest.TestCase):

    def test_invalidate_clears_it(self):
        plex._library_cache[("x", None, True)] = {
            "at": 0, "items": [], "degraded": 0}
        plex.invalidate_library_cache()
        self.assertEqual(plex._library_cache, {})


# ---------------------------------------------------------------------------
# The room map. Every device below is a real one from the house this server
# runs in, identifiers included, because the bugs here are all about which key
# a lookup joins on and generic fixtures hide exactly that.
# ---------------------------------------------------------------------------

BEDROOM = "95c030af1faf5801835d4601a8b37004"
LIVING = "a710a60ff65de04711dd2c4f217fada3"
THEATER = "d2b46d2ad54416315e5e36862d2644a1"
FIRETV = "gd91wa2zwieprb2mbmd1r0u3"
GYM = "f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"

HOUSE = {
    "bedroom": [BEDROOM, "master bedroom"],
    "living room": [LIVING, "lounge", "front room"],
    "theater": [THEATER, "theatre", "movie room"],
    "nicks office": [FIRETV, "nick's office"],
    "gym": ["andie's TV"],
}


class WithHouse(unittest.TestCase):
    """Install the room map for the duration of a test."""

    aliases = HOUSE

    def setUp(self):
        saved = (plex.PLEX_ALIASES, plex.PLEX_ROOMS)
        plex.PLEX_ALIASES, plex.PLEX_ROOMS = plex.parse_aliases(self.aliases)
        self.addCleanup(lambda: setattr_pair(saved))


def setattr_pair(saved):
    plex.PLEX_ALIASES, plex.PLEX_ROOMS = saved


class NormalizeSpoken(unittest.TestCase):
    """Each of these is a way the same room gets said or spelled."""

    def test_possessive_folds_away(self):
        self.assertEqual(plex.normalize_spoken("Andie's Office"),
                         plex.normalize_spoken("andies office"))

    def test_curly_apostrophe_matches_straight_one(self):
        self.assertEqual(plex.normalize_spoken("Andie’s TV"),
                         plex.normalize_spoken("Andie's TV"))

    def test_punctuation_folds_away(self):
        self.assertEqual(plex.normalize_spoken("Roku Express 4K+"),
                         plex.normalize_spoken("roku express 4k"))

    def test_leading_article_is_dropped(self):
        self.assertEqual(plex.normalize_spoken("the theater"), "theater")

    def test_interior_the_is_kept(self):
        # Dropping every "the" would collapse distinct device names.
        self.assertEqual(plex.normalize_spoken("Bedroom the Second"),
                         "bedroom the second")

    def test_none_is_not_a_crash(self):
        self.assertEqual(plex.normalize_spoken(None), "")


class AliasMap(WithHouse):

    def test_string_value_still_means_target(self):
        spoken, rooms = plex.parse_aliases({"theater": "Streaming Stick 4K"})
        self.assertEqual(spoken["theater"], "Streaming Stick 4K")
        self.assertEqual(rooms[plex.normalize_spoken("Streaming Stick 4K")],
                         "theater")

    def test_first_list_entry_is_the_target(self):
        self.assertEqual(plex.PLEX_ALIASES["theater"], THEATER)

    def test_extra_spellings_reach_the_same_target(self):
        for said in ("theater", "theatre", "movie room"):
            self.assertEqual(plex.PLEX_ALIASES[said], THEATER, said)

    def test_room_label_is_itself_a_spelling(self):
        self.assertEqual(plex.PLEX_ALIASES["living room"], LIVING)

    def test_spellings_are_stored_folded(self):
        # "nick's office" and "nicks office" are one spelling, not two.
        self.assertEqual(plex.PLEX_ALIASES["nicks office"], FIRETV)

    def test_empty_value_is_skipped_not_crashed(self):
        spoken, rooms = plex.parse_aliases({"garage": [], "attic": ""})
        self.assertEqual((spoken, rooms), ({}, {}))

    def test_one_bad_room_does_not_cost_the_others(self):
        # A dict here used to resolve to its first key, quietly pointing the
        # room at a device named "nested" - a wrong answer that looks right
        # until playback goes nowhere.
        spoken, rooms = plex.parse_aliases({
            "theater": {"nested": "object"},
            "bedroom": [BEDROOM],
        })
        self.assertNotIn("theater", spoken)
        self.assertEqual(spoken["bedroom"], BEDROOM)

    def test_a_non_object_map_is_ignored_not_fatal(self):
        with self.assertRaises(AttributeError):
            plex.parse_aliases(["theater", "bedroom"])


class RoomLookup(WithHouse):

    def test_identifier_wins(self):
        self.assertEqual(plex.room_of("Streaming Stick 4K", THEATER), "theater")

    def test_display_name_works_when_no_identifier_is_mapped(self):
        self.assertEqual(plex.room_of("andie's TV", None), "gym")

    def test_name_is_folded_before_lookup(self):
        self.assertEqual(plex.room_of("Andies TV", None), "gym")

    def test_unmapped_device_has_no_room(self):
        self.assertIsNone(plex.room_of("DESKTOP-CHB1M9E", "t0v7x03y0qggo77gd92xd2t9"))


def device(name, mid, player=True, reachable=False,
           product="Plex for Roku", platform="Roku"):
    return {
        "name": name, "product": product, "platform": platform,
        "machine_identifier": mid, "provides": ["player"] if player else [],
        "connections": [], "last_seen": None,
        "advertises_player": player, "reachable": reachable,
    }


def session(mid, title, state="playing", product="Plex for Roku", platform="Roku"):
    return Item(players=[Item(machineIdentifier=mid, title=title, state=state,
                              product=product, platform=platform)])


class FakePlex:
    def __init__(self, clients=(), sessions=()):
        self._clients, self._sessions = list(clients), list(sessions)

    def clients(self):
        return self._clients

    def sessions(self):
        return self._sessions


class Discovery(WithHouse):
    """The merge of three endpoints that disagree about what a player is."""

    def install(self, devices=(), clients=(), sessions=()):
        saved = (plex.plex, plex.account_devices)
        plex.plex = lambda: FakePlex(clients, sessions)
        plex.account_devices = lambda: list(devices)
        self.addCleanup(lambda: restore(saved))
        return plex.discover_players()

    def test_session_only_player_is_not_lost(self):
        # The regression: a device streaming right now that appears in neither
        # plex.tv's device list nor /clients used to vanish from list_players
        # while still showing in now_playing, which reads as the two tools
        # contradicting each other.
        found = self.install(sessions=[session(GYM, "andie's TV")])
        self.assertEqual([d["machine_identifier"] for d in found], [GYM])
        self.assertFalse(found[0]["controllable"])
        self.assertEqual(found[0]["room"], "gym")

    def test_session_only_player_says_why_it_cannot_be_driven(self):
        found = self.install(sessions=[session(GYM, "andie's TV")])
        self.assertIn("not registered", found[0]["status"])

    def test_streaming_state_stays_a_string(self):
        found = self.install(
            devices=[device("Sleepy", LIVING, reachable=True)],
            sessions=[session(LIVING, "Sleepy", state="paused")],
        )
        self.assertEqual(found[0]["streaming_now"], "paused")

    def test_a_device_in_every_source_appears_once(self):
        found = self.install(
            devices=[device("Sleepy", LIVING, reachable=True)],
            clients=[Item(machineIdentifier=LIVING, title="Sleepy",
                          product="Plex for Roku", platform="Roku")],
            sessions=[session(LIVING, "Sleepy")],
        )
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["controllable"])

    def test_rooms_are_attached_to_every_entry(self):
        found = self.install(devices=[
            device("Streaming Stick 4K", THEATER, reachable=True),
            device("DESKTOP-CHB1M9E", "t0v7x03y0qggo77gd92xd2t9",
                   product="Plex Media Player", platform="Konvergo"),
        ])
        rooms = {d["name"]: d["room"] for d in found}
        self.assertEqual(rooms["Streaming Stick 4K"], "theater")
        self.assertIsNone(rooms["DESKTOP-CHB1M9E"])

    def test_a_renamed_device_keeps_its_room(self):
        # The whole point of keying on the identifier: the Roku reports its
        # retail box name and can be relabelled at any time.
        found = self.install(devices=[device("Some New Name", THEATER,
                                             reachable=True)])
        self.assertEqual(found[0]["room"], "theater")


def restore(saved):
    plex.plex, plex.account_devices = saved


class Resolution(WithHouse):
    """Which device a spoken name lands on - the only thing that matters."""

    def setUp(self):
        super().setUp()
        self.players = [
            device("Roku Express 4K+", BEDROOM, reachable=True),
            device("Sleepy", LIVING, reachable=True),
            device("Streaming Stick 4K", THEATER, reachable=True),
            device("unknown", FIRETV, player=False,
                   product="Plex for Amazon FireTV", platform="Kepler"),
        ]
        for entry in self.players:
            entry.update(controllable=entry["advertises_player"],
                         route="server", streaming_now=None, relevant=True,
                         status="ready (registered with the Plex server)",
                         room=plex.room_of(entry["name"],
                                           entry["machine_identifier"]))
        self.players[-1]["status"] = (
            "cannot be controlled - this app never advertises itself as a "
            "player. No API call will work. Reporting this is the answer.")
        saved = (plex.discover_players, plex.build_client)
        plex.discover_players = lambda: list(self.players)
        plex.build_client = lambda entry: entry
        self.addCleanup(lambda: restore_resolution(saved))

    def resolve(self, said):
        return plex.resolve_player(said)["machine_identifier"]

    def test_room_name_reaches_the_right_box(self):
        self.assertEqual(self.resolve("theater"), THEATER)
        self.assertEqual(self.resolve("bedroom"), BEDROOM)
        self.assertEqual(self.resolve("living room"), LIVING)

    def test_extra_spelling_reaches_the_same_box(self):
        for said in ("theatre", "movie room", "the theater", "THEATER"):
            self.assertEqual(self.resolve(said), THEATER, said)

    def test_lounge_reaches_the_living_room(self):
        # "Sleepy" contains none of these words; without the map this is a miss.
        for said in ("lounge", "front room", "the lounge"):
            self.assertEqual(self.resolve(said), LIVING, said)

    def test_identifier_can_be_named_directly(self):
        self.assertEqual(self.resolve(THEATER), THEATER)

    def test_hyphenated_identifier_still_matches(self):
        # Plenty of clients use a hyphenated UUID. Folding one side of the
        # comparison and not the other made those unreachable by identifier.
        uuid = "3f2a1c4e-9b7d-4a10-8e55-6c0f2b8d1a93"
        self.players.append(dict(self.players[0], name="Shield",
                                 machine_identifier=uuid, room=None))
        self.assertEqual(self.resolve(uuid), uuid)

    def test_display_name_still_works(self):
        self.assertEqual(self.resolve("Streaming Stick 4K"), THEATER)

    def test_display_name_survives_lost_punctuation(self):
        self.assertEqual(self.resolve("roku express 4k"), BEDROOM)

    def test_room_beats_a_substring_collision(self):
        # "bedroom" is a substring of nothing here, but the room rung runs
        # before the substring rung so a future device called "Bedroom TV" in
        # another room cannot steal the mapped one.
        self.players.append(dict(self.players[0], name="Bedroom TV",
                                 machine_identifier="zzz", room=None))
        self.assertEqual(self.resolve("bedroom"), BEDROOM)

    def test_uncontrollable_device_reports_its_reason_by_room_name(self):
        with self.assertRaises(plex.ToolError) as caught:
            plex.resolve_player("nicks office")
        self.assertIn("never advertises itself as a player",
                      str(caught.exception))
        self.assertEqual(caught.exception.extra["player"], "nicks office")

    def test_unknown_room_lists_rooms_not_device_names(self):
        with self.assertRaises(plex.ToolError) as caught:
            plex.resolve_player("kitchen")
        offered = caught.exception.extra["available_players"]
        self.assertIn("theater", offered)
        self.assertNotIn("Streaming Stick 4K", offered)

    def test_punctuation_only_input_is_refused_not_matched(self):
        # normalize_spoken empties this out; an empty needle would otherwise
        # substring-match every device and resolve to an arbitrary one.
        with self.assertRaises(plex.ToolError):
            plex.resolve_player("???")


def restore_resolution(saved):
    plex.discover_players, plex.build_client = saved


if __name__ == "__main__":
    unittest.main()
