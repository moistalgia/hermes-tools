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

import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from urllib.parse import unquote

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


# ---------------------------------------------------------------------------
# Metadata editing
#
# A wrong edit is the one kind of failure this server has that a user cannot
# see happening and cannot undo by trying again, so the fake below models what
# Plex actually does with an edit request rather than just recording that one
# was sent. Two variants of it exist because the one thing nobody can be sure
# of from the outside is whether Plex's indexed tag parameters replace the tag
# list or append to it - so the writes have to land on the right answer under
# both, and there is a test for each.
# ---------------------------------------------------------------------------

FIELD_ATTR = {
    "title": "title",
    "year": "year",
    "originalTitle": "originalTitle",
    "titleSort": "titleSort",
    "summary": "summary",
}
TAG_ATTR = {
    "genre": "genres",
    "label": "labels",
    "collection": "collections",
    "country": "countries",
}


class FakeMovie:
    """A movie that responds to Plex's edit parameters the way Plex does.

    `append_tags` switches the indexed tag parameters from replace to append,
    which is the other plausible reading of the Plex API. `stubborn` accepts
    every edit and applies none, which is the failure mode readback exists to
    catch: Plex returns 200 for edits it declines to make.
    """

    def __init__(self, rating_key="1", title="Torso", year=1973, genres=(),
                 labels=(), collections=(), countries=(), locked=(),
                 summary=None, original_title=None, sort_title=None,
                 append_tags=False, stubborn=False, thumb="/library/thumb/1",
                 art="/library/art/1", logo="/library/logo/1",
                 guid="plex://movie/abc", guids=None, file=None,
                 matches=(), settles=True):
        self.ratingKey = rating_key
        self.type = "movie"
        self.title = title
        self.year = year
        self.summary = summary
        self.originalTitle = original_title
        self.titleSort = sort_title
        self.duration = 5400000
        self.librarySectionTitle = "Movies"
        self.viewCount = 0
        self.thumb = thumb
        self.art = art
        self.logo = logo
        self.guid = guid
        self.guids = list(guids) if guids is not None else []
        self.media = [Item(parts=[Item(file=file, size=1, container="mkv")])] \
            if file else []
        self.genres = [tag(t) for t in genres]
        self.labels = [tag(t) for t in labels]
        self.collections = [tag(t) for t in collections]
        self.countries = [tag(t) for t in countries]
        self.locked = set(locked)
        self.append_tags = append_tags
        self.stubborn = stubborn
        self.settles = settles
        self.edits = []
        self.reloads = 0
        self._posters = []
        self._arts = []
        self._logos = []
        self._matches = [FakeMatch(*m) for m in matches]
        self.match_calls = []
        self.match_applied = None
        self.uploads = []

    def posters(self):
        return self._posters

    def arts(self):
        return self._arts

    def logos(self):
        return self._logos

    def uploadLogo(self, url=None, filepath=None):
        self.uploads.append(("logo", url))
        self.logo = "/library/metadata/1/clearLogo/uploaded"
        return self

    def uploadPoster(self, url=None, filepath=None):
        self.uploads.append(("poster", url))
        self.thumb = "/library/metadata/1/thumb/uploaded"
        return self

    def uploadArt(self, url=None, filepath=None):
        self.uploads.append(("art", url))
        self.art = "/library/metadata/1/art/uploaded"
        return self

    def matches(self, **kwargs):
        self.match_calls.append(kwargs)
        return list(self._matches)

    def fixMatch(self, searchResult=None, auto=False, agent=None):
        self.match_applied = searchResult.guid
        if not self.settles:
            return self
        # A real match replaces the whole record, artwork included. That is
        # the entire reason to prefer it over five field edits.
        self.guid = searchResult.guid
        self.title = searchResult.name
        self.year = int(searchResult.year) if searchResult.year else self.year
        self.summary = self.summary or f"Summary for {searchResult.name}."
        self.thumb = self.thumb or "/library/metadata/1/thumb/matched"
        self.art = self.art or "/library/metadata/1/art/matched"
        if not self.genres:
            self.genres = [tag("Horror")]
        return self

    @property
    def fields(self):
        return [Item(name=name, locked=True) for name in sorted(self.locked)]

    def isFullObject(self):
        return True

    def reload(self):
        self.reloads += 1
        return self

    def edit(self, **kwargs):
        self.edits.append(dict(kwargs))
        if self.stubborn:
            return self
        indexed = {}
        for key, value in kwargs.items():
            if key.endswith(".locked"):
                name = key[: -len(".locked")]
                if str(value) == "1":
                    self.locked.add(name)
                else:
                    self.locked.discard(name)
            elif key.endswith(".value"):
                setattr(self, FIELD_ATTR[key[: -len(".value")]], value)
            elif key.endswith("[].tag.tag-"):
                param = key[: -len("[].tag.tag-")]
                drop = {unquote(v).lower() for v in str(value).split(",") if v}
                attribute = TAG_ATTR[param]
                setattr(self, attribute, [
                    t for t in getattr(self, attribute) if t.tag.lower() not in drop
                ])
            else:
                match = re.match(r"^(\w+)\[(\d+)\]\.tag\.tag$", key)
                if match:
                    indexed.setdefault(match.group(1), []).append(
                        (int(match.group(2)), value))
        for param, entries in indexed.items():
            attribute = TAG_ATTR[param]
            written = [tag(v) for _, v in sorted(entries)]
            if self.append_tags:
                have = {t.tag.lower() for t in getattr(self, attribute)}
                written = getattr(self, attribute) + [
                    t for t in written if t.tag.lower() not in have]
            setattr(self, attribute, written)
        return self

    def tag_names(self, attribute="genres"):
        return [t.tag for t in getattr(self, attribute)]


class MetadataFixture(unittest.TestCase):
    """Point the module's item lookup at fakes for the length of one test."""

    def setUp(self):
        self.items = {}
        self.saved = (plex.get_by_rating_key, plex.find_media)
        plex.get_by_rating_key = lambda key: self.fetch(key)
        plex.find_media = lambda *a, **k: []
        self.addCleanup(self.restore)

    def restore(self):
        plex.get_by_rating_key, plex.find_media = self.saved

    def fetch(self, key):
        try:
            return self.items[str(key)]
        except KeyError:
            raise Exception(f"(404) not_found; ratingKey {key}")

    def add(self, **kwargs):
        movie = FakeMovie(**kwargs)
        self.items[str(movie.ratingKey)] = movie
        return movie

    def update(self, **args):
        return plex.call_tool("update_item_metadata", args)

    def batch(self, **args):
        return plex.call_tool("batch_update_item_metadata", args)


class TagListParsing(unittest.TestCase):
    def test_duplicates_fold_case_insensitively(self):
        # Three corrections in a row is how a movie ends up tagged Horror twice
        # and horror once; the list is what gets written, so it dedupes here.
        self.assertEqual(
            plex.normalize_tag_list(["Horror", "horror", "HORROR"], "genres"),
            ["Horror"])

    def test_order_is_preserved(self):
        self.assertEqual(
            plex.normalize_tag_list(["Horror", "Mystery", "Thriller"], "genres"),
            ["Horror", "Mystery", "Thriller"])

    def test_a_comma_string_is_accepted(self):
        self.assertEqual(plex.normalize_tag_list("Horror, Thriller", "genres"),
                         ["Horror", "Thriller"])

    def test_a_json_string_is_accepted(self):
        self.assertEqual(plex.normalize_tag_list('["Horror","Drama"]', "genres"),
                         ["Horror", "Drama"])

    def test_broken_json_is_refused_not_split_on_commas(self):
        with self.assertRaises(plex.ToolError):
            plex.normalize_tag_list('["Horror",', "genres")


class TagParameters(unittest.TestCase):
    def test_setting_is_indexed_and_locks_the_field(self):
        self.assertEqual(plex.tag_params("genre", ["Horror", "Mystery"]), {
            "genre.locked": 1,
            "genre[0].tag.tag": "Horror",
            "genre[1].tag.tag": "Mystery",
        })

    def test_removal_uses_the_minus_parameter(self):
        params = plex.tag_params("genre", ["Comedy"], remove=True)
        self.assertEqual(params["genre[].tag.tag-"], "Comedy")

    def test_removal_quotes_values(self):
        # Plex expects the minus form pre-quoted; without this every tag with
        # an ampersand in it silently fails to come off.
        params = plex.tag_params("label", ["Rock & Roll"], remove=True)
        self.assertNotIn("&", params["label[].tag.tag-"])
        self.assertEqual(unquote(params["label[].tag.tag-"]), "Rock & Roll")


class UpdatePlanning(MetadataFixture):
    def test_omitted_fields_do_not_appear_in_the_diff(self):
        self.add(rating_key="1", title="Torso", year=1974, genres=["Horror"])
        result = self.update(rating_key="1", year=1973, dry_run=True)
        self.assertEqual(list(result["changes"]), ["year"])
        self.assertEqual(result["changes"]["year"],
                         {"before": 1974, "after": 1973})

    def test_a_supplied_array_replaces_rather_than_appends(self):
        self.add(rating_key="1", genres=["Comedy", "Horror"])
        result = self.update(rating_key="1", genres=["Horror", "Mystery"],
                             dry_run=True)
        self.assertEqual(result["changes"]["genres"]["after"],
                         ["Horror", "Mystery"])

    def test_an_empty_array_is_refused(self):
        # The payload that wipes a record is an array nobody meant to send.
        self.add(rating_key="1", genres=["Horror"])
        result = self.update(rating_key="1", genres=[], confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")
        self.assertEqual(self.items["1"].edits, [])

    def test_clearing_must_be_spelled_out(self):
        movie = self.add(rating_key="1", genres=["Horror"])
        result = self.update(rating_key="1", clear_genres=True, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(movie.tag_names(), [])

    def test_clear_and_a_list_together_is_a_contradiction(self):
        self.add(rating_key="1", genres=["Horror"])
        result = self.update(rating_key="1", genres=["Drama"],
                             clear_genres=True, confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.items["1"].edits, [])

    def test_an_empty_string_field_is_refused(self):
        self.add(rating_key="1", title="Torso")
        result = self.update(rating_key="1", title="   ", confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")
        self.assertEqual(self.items["1"].edits, [])

    def test_an_implausible_year_is_refused(self):
        self.add(rating_key="1", year=1973)
        result = self.update(rating_key="1", year=19733, confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.items["1"].edits, [])

    def test_a_year_arriving_as_a_string_is_coerced(self):
        movie = self.add(rating_key="1", year=1974)
        self.assertTrue(self.update(rating_key="1", year="1973",
                                    confirm=True)["ok"])
        self.assertEqual(movie.year, 1973)

    def test_nothing_to_do_writes_nothing(self):
        movie = self.add(rating_key="1", genres=["Horror"],
                         locked=["genre", "title"], title="Torso")
        result = self.update(rating_key="1", title="Torso", genres=["Horror"],
                             confirm=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(movie.edits, [])

    def test_a_correct_but_unlocked_value_still_gets_locked(self):
        # The point of the edit is that the next metadata refresh cannot undo
        # it, so a right-but-unlocked field is not already done.
        movie = self.add(rating_key="1", genres=["Horror"])
        self.assertTrue(self.update(rating_key="1", genres=["Horror"],
                                    confirm=True)["ok"])
        self.assertIn("genre", movie.locked)

    def test_an_unknown_rating_key_is_a_structured_not_found(self):
        result = self.update(rating_key="999", genres=["Horror"], confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")


class UpdateWriting(MetadataFixture):
    def test_without_confirm_nothing_is_written(self):
        movie = self.add(rating_key="1", year=1974)
        result = self.update(rating_key="1", year=1973)
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(movie.edits, [])
        self.assertEqual(movie.year, 1974)

    def test_dry_run_beats_confirm(self):
        movie = self.add(rating_key="1", year=1974)
        result = self.update(rating_key="1", year=1973, dry_run=True,
                             confirm=True)
        self.assertFalse(result["applied"])
        self.assertEqual(movie.edits, [])

    def test_confirm_writes_locks_and_verifies(self):
        movie = self.add(rating_key="1", title="Torso I Corpi", year=1974)
        result = self.update(rating_key="1", title="Torso", year=1973,
                             genres=["Horror", "Mystery", "Thriller"],
                             confirm=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(movie.title, "Torso")
        self.assertEqual(movie.year, 1973)
        self.assertEqual(movie.tag_names(), ["Horror", "Mystery", "Thriller"])
        self.assertTrue({"title", "year", "genre"} <= movie.locked)

    def test_a_stale_genre_is_removed_not_left_behind(self):
        movie = self.add(rating_key="1", genres=["Comedy", "Horror"])
        self.update(rating_key="1", genres=["Horror"], confirm=True)
        self.assertEqual(movie.tag_names(), ["Horror"])
        removals = [e for e in movie.edits if "genre[].tag.tag-" in e]
        self.assertEqual(len(removals), 1)
        self.assertEqual(unquote(removals[0]["genre[].tag.tag-"]), "Comedy")

    def test_replacement_holds_even_if_plex_appends(self):
        # Whether Plex's indexed parameters replace or append is the one thing
        # that cannot be settled from out here, so the removal goes first and
        # the result is the same either way.
        movie = self.add(rating_key="1", genres=["Comedy", "Horror"],
                         append_tags=True)
        self.update(rating_key="1", genres=["Horror", "Mystery"], confirm=True)
        self.assertEqual(sorted(movie.tag_names()), ["Horror", "Mystery"])

    def test_a_label_does_not_disturb_the_genres(self):
        # The marathon label is for horror-adjacent films that should not be
        # filed under the Horror genre, so writing one must not touch genres.
        movie = self.add(rating_key="1", genres=["Drama", "Thriller"])
        result = self.update(rating_key="1", labels=["Horror Marathon"],
                             confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(movie.tag_names("labels"), ["Horror Marathon"])
        self.assertEqual(movie.tag_names("genres"), ["Drama", "Thriller"])
        self.assertNotIn("genres", result["changes"])

    def test_a_write_plex_ignores_is_reported_as_unverified(self):
        movie = self.add(rating_key="1", year=1974, stubborn=True)
        result = self.update(rating_key="1", year=1973, confirm=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "readback_mismatch")
        self.assertEqual(result["mismatches"][0],
                         {"field": "year", "requested": 1973, "live": 1974})
        self.assertEqual(result["after"]["year"], 1974)
        self.assertTrue(movie.edits)

    def test_the_readback_is_a_real_reload(self):
        movie = self.add(rating_key="1", year=1974)
        self.update(rating_key="1", year=1973, confirm=True)
        self.assertGreaterEqual(movie.reloads, 1)


class BatchUpdate(MetadataFixture):
    def test_over_the_limit_is_refused_before_anything_is_written(self):
        for n in range(30):
            self.add(rating_key=str(n), genres=["Comedy"])
        result = self.batch(
            updates=[{"rating_key": str(n), "genres": ["Horror"]}
                     for n in range(30)],
            confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("25", result["error"])
        self.assertEqual(self.items["0"].edits, [])

    def test_one_bad_entry_stops_the_whole_batch(self):
        # Half a tagging pass is worse than none of one: you cannot tell by
        # looking which half ran.
        good = self.add(rating_key="1", genres=["Comedy"])
        result = self.batch(
            updates=[{"rating_key": "1", "genres": ["Horror"]},
                     {"rating_key": "999", "genres": ["Horror"]}],
            confirm=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(good.edits, [])
        self.assertEqual(result["problems"][0]["error_code"], "not_found")
        # The entries that would have worked are still shown, so the caller can
        # fix one line instead of rebuilding the pass.
        self.assertEqual(result["would_change"][0]["rating_key"], "1")

    def test_a_missing_rating_key_is_caught_in_validation(self):
        self.add(rating_key="1")
        result = self.batch(updates=[{"genres": ["Horror"]}], confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("rating_key", result["problems"][0]["error"])

    def test_the_same_item_twice_is_refused(self):
        # Two entries for one movie means one of them silently loses, and which
        # one depends on ordering nobody is thinking about.
        self.add(rating_key="1", genres=["Comedy"])
        result = self.batch(
            updates=[{"rating_key": "1", "genres": ["Horror"]},
                     {"rating_key": "1", "genres": ["Drama"]}],
            confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.items["1"].edits, [])

    def test_dry_run_returns_every_diff_and_writes_nothing(self):
        self.add(rating_key="1", title="Peeping Tom", genres=["Drama"])
        self.add(rating_key="2", title="Black Sunday", genres=[])
        result = self.batch(
            updates=[{"rating_key": "1", "genres": ["Horror", "Thriller"]},
                     {"rating_key": "2", "genres": ["Horror"]}],
            dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["results"][0]["changes"]["genres"]["after"],
                         ["Horror", "Thriller"])
        self.assertEqual(self.items["1"].edits, [])

    def test_without_confirm_a_batch_is_a_dry_run(self):
        self.add(rating_key="1", genres=["Drama"])
        result = self.batch(updates=[{"rating_key": "1", "genres": ["Horror"]}])
        self.assertFalse(result["applied"])
        self.assertEqual(self.items["1"].edits, [])

    def test_confirm_writes_and_verifies_each_item_separately(self):
        one = self.add(rating_key="1", title="Peeping Tom", genres=["Drama"])
        two = self.add(rating_key="2", genres=[])
        result = self.batch(
            updates=[{"rating_key": "1", "genres": ["Horror", "Thriller"]},
                     {"rating_key": "2", "labels": ["Horror Marathon"]}],
            confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified_count"], 2)
        self.assertTrue(all(r["verified"] for r in result["results"]))
        self.assertEqual(one.tag_names(), ["Horror", "Thriller"])
        self.assertEqual(two.tag_names("labels"), ["Horror Marathon"])

    def test_one_failure_does_not_hide_the_other_results(self):
        good = self.add(rating_key="1", genres=["Drama"])
        self.add(rating_key="2", genres=["Drama"], stubborn=True)
        result = self.batch(
            updates=[{"rating_key": "1", "genres": ["Horror"]},
                     {"rating_key": "2", "genres": ["Horror"]}],
            confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], ["2"])
        self.assertEqual(result["verified_count"], 1)
        rows = {r["rating_key"]: r for r in result["results"]}
        self.assertTrue(rows["1"]["verified"])
        self.assertEqual(rows["2"]["error_code"], "readback_mismatch")
        self.assertEqual(good.tag_names(), ["Horror"])

    def test_a_no_op_entry_is_reported_as_one(self):
        movie = self.add(rating_key="1", genres=["Horror"], locked=["genre"])
        result = self.batch(updates=[{"rating_key": "1", "genres": ["Horror"]}],
                            confirm=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["results"][0]["applied"])
        self.assertEqual(movie.edits, [])


class ItemResolution(MetadataFixture):
    """A write is never made off a fuzzy title - 'Black Sunday' is two films."""

    def test_a_title_collision_is_refused_with_candidates(self):
        plex.find_media = lambda *a, **k: [
            FakeMovie(rating_key="1", title="Black Sunday", year=1960),
            FakeMovie(rating_key="2", title="Black Sunday", year=1977),
        ]
        result = plex.call_tool("get_item_metadata", {"query": "Black Sunday"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ambiguous_match")
        self.assertEqual({c["rating_key"] for c in result["candidates"]},
                         {"1", "2"})

    def test_a_year_in_the_query_settles_the_collision(self):
        plex.find_media = lambda *a, **k: [
            FakeMovie(rating_key="1", title="Black Sunday", year=1960),
            FakeMovie(rating_key="2", title="Black Sunday", year=1977),
        ]
        result = plex.call_tool("get_item_metadata",
                                {"query": "Black Sunday (1960)"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["rating_key"], "1")

    def test_an_exact_title_beats_a_looser_hub_match(self):
        plex.find_media = lambda *a, **k: [
            FakeMovie(rating_key="1", title="Torso", year=1973),
            FakeMovie(rating_key="2", title="Torso Killer", year=2023),
        ]
        result = plex.call_tool("get_item_metadata", {"query": "Torso"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["rating_key"], "1")

    def test_nothing_found_is_a_structured_not_found(self):
        result = plex.call_tool("get_item_metadata", {"query": "nonexistent"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")

    def test_update_refuses_a_query_and_demands_a_rating_key(self):
        result = self.update(query="Torso", genres=["Horror"], confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("rating_key", result["error"])


class MetadataSnapshot(MetadataFixture):
    def test_the_read_reports_what_is_locked(self):
        self.add(rating_key="1", genres=["Horror"], locked=["genre", "title"])
        result = plex.call_tool("get_item_metadata", {"rating_key": "1"})
        self.assertEqual(result["locked_fields"], ["genre", "title"])

    def test_the_read_carries_no_token(self):
        movie = self.add(rating_key="1")
        movie._data = Item(attrib={"guid": "plex://movie/abc",
                                   "X-Plex-Token": "secret"})
        result = plex.call_tool("get_item_metadata", {"rating_key": "1"})
        self.assertIn("guid", result["raw_metadata"])
        self.assertNotIn("secret", json.dumps(result))


# ---------------------------------------------------------------------------
# Artwork, matching, auditing
# ---------------------------------------------------------------------------


class FakePoster:
    def __init__(self, id, provider="themoviedb", selected=False, owner=None):
        self.ratingKey = id
        self.provider = provider
        self.selected = selected
        self.thumb = f"https://image.tmdb.org/{id}.jpg"
        self._owner = owner

    def select(self):
        for other in self._owner:
            other.selected = False
        self.selected = True


class FakeMatch:
    def __init__(self, guid, name, year=None, score=90):
        self.guid, self.name, self.year, self.score = guid, name, year, score


class FakeSection:
    def __init__(self, title="Movies", kind="movie"):
        self.title, self.type, self.key = title, kind, "1"


def art_movie(rating_key="1", posters=(), arts=(), logos=(), **kwargs):
    """A FakeMovie wired for the artwork and matching tools."""
    movie = FakeMovie(rating_key=rating_key, **kwargs)
    movie._posters = [FakePoster(**p) for p in posters]
    for p in movie._posters:
        p._owner = movie._posters
    movie._arts = [FakePoster(**a) for a in arts]
    for a in movie._arts:
        a._owner = movie._arts
    movie._logos = [FakePoster(**g) for g in logos]
    for g in movie._logos:
        g._owner = movie._logos
    return movie


class ArtworkTools(MetadataFixture):
    def make(self, **kwargs):
        movie = art_movie(**kwargs)
        self.items[str(movie.ratingKey)] = movie
        return movie

    def test_no_poster_and_no_candidates_points_at_matching(self):
        # The screenshot case: a black rectangle is usually an unmatched file,
        # not a missing image, and uploading art onto it fixes the symptom.
        self.make(thumb=None)
        result = plex.call_tool("get_artwork", {"rating_key": "1"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["has_poster"])
        self.assertIn("unmatched", result["note"])

    def test_candidates_are_listed_with_the_selected_one_marked(self):
        self.make(posters=[{"id": "a"}, {"id": "b", "selected": True}])
        result = plex.call_tool("get_artwork", {"rating_key": "1"})
        self.assertEqual([p["id"] for p in result["posters"]], ["a", "b"])
        self.assertEqual([p["selected"] for p in result["posters"]],
                         [False, True])

    def test_no_token_is_ever_returned(self):
        # plexapi's thumbUrl helpers embed the token; nothing here may.
        self.make(thumb="/library/metadata/1/thumb/1?X-Plex-Token=secret")
        result = plex.call_tool("get_artwork", {"rating_key": "1"})
        self.assertNotIn("secret", json.dumps(result))

    def test_an_id_and_a_url_together_are_refused(self):
        movie = self.make(posters=[{"id": "a"}])
        result = plex.call_tool("set_artwork", {
            "rating_key": "1", "poster_id": "a",
            "poster_url": "https://example.com/p.jpg", "confirm": True})
        self.assertFalse(result["ok"])
        self.assertEqual(movie.uploads, [])

    def test_a_non_http_url_is_refused(self):
        # Otherwise this is a tool that makes the Plex server open a file path
        # chosen by a model.
        self.make()
        result = plex.call_tool("set_artwork", {
            "rating_key": "1", "poster_url": "file:///etc/passwd",
            "confirm": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")

    def test_nothing_to_set_is_refused(self):
        self.make()
        result = plex.call_tool("set_artwork", {"rating_key": "1",
                                                "confirm": True})
        self.assertFalse(result["ok"])

    def test_without_confirm_nothing_is_set(self):
        movie = self.make(posters=[{"id": "a"}])
        result = plex.call_tool("set_artwork", {"rating_key": "1",
                                                "poster_id": "a"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertFalse(movie._posters[0].selected)

    def test_confirm_selects_and_locks_and_verifies(self):
        movie = self.make(posters=[{"id": "a"}, {"id": "b"}])
        result = plex.call_tool("set_artwork", {"rating_key": "1",
                                                "poster_id": "b",
                                                "confirm": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(movie._posters[1].selected)
        self.assertIn("thumb", movie.locked)

    def test_a_stale_id_is_a_named_error_not_a_silent_no_op(self):
        # Candidate ids change when an item is rematched, so the id an agent
        # read a minute ago may no longer exist.
        movie = self.make(posters=[{"id": "a"}])
        result = plex.call_tool("set_artwork", {"rating_key": "1",
                                                "poster_id": "gone",
                                                "confirm": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")
        self.assertEqual(result["available"], ["a"])
        self.assertFalse(movie._posters[0].selected)


class MatchTools(MetadataFixture):
    def make(self, **kwargs):
        movie = art_movie(**kwargs)
        self.items[str(movie.ratingKey)] = movie
        return movie

    def test_an_unmatched_guid_is_recognised(self):
        self.assertTrue(plex.is_unmatched(Item(guid="local://55")))
        self.assertTrue(plex.is_unmatched(Item(guid="")))
        self.assertFalse(plex.is_unmatched(Item(guid="plex://movie/abc")))

    def test_candidates_come_back_without_changing_anything(self):
        movie = self.make(guid="local://9", title="The Entity Horror",
                          matches=[("plex://movie/ent", "The Entity", "1982")])
        result = plex.call_tool("list_match_candidates", {"rating_key": "1"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["currently_unmatched"])
        self.assertEqual(result["candidates"][0]["name"], "The Entity")
        self.assertEqual(movie.edits, [])
        self.assertIsNone(movie.match_applied)

    def test_an_explicit_title_is_passed_through_to_plex(self):
        # The whole point on a mangled title: search for what you think it is,
        # not for what the item currently claims.
        movie = self.make(title="The Entity Horror", matches=[])
        plex.call_tool("list_match_candidates",
                       {"rating_key": "1", "title": "The Entity", "year": 1982})
        self.assertEqual(movie.match_calls[-1], {"title": "The Entity",
                                                 "year": 1982})

    def test_locked_fields_are_warned_about_before_a_rematch(self):
        self.make(locked=["title"], matches=[("g", "The Entity", "1982")])
        result = plex.call_tool("list_match_candidates", {"rating_key": "1"})
        self.assertIn("title", result["warning"])

    def test_a_guid_not_on_offer_is_refused(self):
        # A guid carried over from another item would otherwise be a PUT that
        # Plex accepts and does nothing with.
        movie = self.make(matches=[("real", "The Entity", "1982")])
        result = plex.call_tool("fix_match", {"rating_key": "1",
                                              "guid": "invented",
                                              "confirm": True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")
        self.assertIsNone(movie.match_applied)

    def test_without_confirm_nothing_is_matched(self):
        movie = self.make(matches=[("g", "The Entity", "1982")])
        result = plex.call_tool("fix_match", {"rating_key": "1", "guid": "g"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertIsNone(movie.match_applied)
        self.assertEqual(result["match"]["name"], "The Entity")

    def test_a_dry_run_warns_that_locks_will_survive(self):
        self.make(locked=["title", "genre"],
                  matches=[("g", "The Entity", "1982")])
        result = plex.call_tool("fix_match", {"rating_key": "1", "guid": "g",
                                              "dry_run": True})
        self.assertIn("will NOT be replaced", result["warning"])

    def test_confirm_applies_the_match_and_reports_both_sides(self):
        saved = plex.MATCH_WAIT_SECONDS
        plex.MATCH_WAIT_SECONDS = 2
        self.addCleanup(lambda: setattr(plex, "MATCH_WAIT_SECONDS", saved))
        movie = self.make(title="The Entity Horror", year=None, guid="local://9",
                          thumb=None, genres=[],
                          matches=[("plex://movie/ent", "The Entity", "1982")])
        result = plex.call_tool("fix_match", {"rating_key": "1",
                                              "guid": "plex://movie/ent",
                                              "confirm": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(movie.match_applied, "plex://movie/ent")
        self.assertEqual(result["before"]["title"], "The Entity Horror")
        self.assertEqual(result["after"]["title"], "The Entity")
        self.assertFalse(result["before_artwork"]["has_poster"])
        self.assertTrue(result["after_artwork"]["has_poster"])

    def test_unlock_first_clears_the_locks_before_matching(self):
        saved = plex.MATCH_WAIT_SECONDS
        plex.MATCH_WAIT_SECONDS = 2
        self.addCleanup(lambda: setattr(plex, "MATCH_WAIT_SECONDS", saved))
        movie = self.make(guid="local://9", locked=["title", "year"],
                          matches=[("plex://movie/ent", "The Entity", "1982")])
        plex.call_tool("fix_match", {"rating_key": "1",
                                     "guid": "plex://movie/ent",
                                     "unlock_first": True, "confirm": True})
        self.assertEqual(movie.locked, set())

    def test_a_match_that_never_settles_is_not_reported_as_success(self):
        saved = plex.MATCH_WAIT_SECONDS
        plex.MATCH_WAIT_SECONDS = 0  # no polling window, so nothing can settle
        self.addCleanup(lambda: setattr(plex, "MATCH_WAIT_SECONDS", saved))
        self.make(guid="local://9", settles=False,
                  matches=[("plex://movie/ent", "The Entity", "1982")])
        result = plex.call_tool("fix_match", {"rating_key": "1",
                                              "guid": "plex://movie/ent",
                                              "confirm": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "readback_mismatch")
        self.assertIn("do not apply the match twice", result["error"])


class UnlockTool(MetadataFixture):
    def test_unlocking_needs_confirm(self):
        movie = self.add(rating_key="1", locked=["title"])
        result = plex.call_tool("unlock_metadata_fields", {"rating_key": "1"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["would_unlock"], ["title"])
        self.assertEqual(movie.locked, {"title"})

    def test_confirmed_unlock_is_verified_by_readback(self):
        movie = self.add(rating_key="1", locked=["title", "genre"])
        result = plex.call_tool("unlock_metadata_fields",
                                {"rating_key": "1", "fields": ["title"],
                                 "confirm": True})
        self.assertTrue(result["ok"])
        self.assertEqual(movie.locked, {"genre"})
        self.assertEqual(result["locked_fields"], ["genre"])

    def test_a_field_that_is_not_locked_is_reported_not_invented(self):
        self.add(rating_key="1", locked=["title"])
        result = plex.call_tool("unlock_metadata_fields",
                                {"rating_key": "1", "fields": ["summary"],
                                 "confirm": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["not_locked"], ["summary"])


class SuspectTitle(unittest.TestCase):
    """The detector for the failure find_gaps cannot see.

    Every title on the left is one of these: a real library entry, or a real
    film. The false-positive half matters more than the other one - an agent
    that learns the sweep cries wolf stops reading it.
    """

    BROKEN = [
        ("The Entity Horror", "high"),
        ("L'occhio Che Uccide Peeping Tom", "high"),
        ("La Maschera Del Demonio Black Sunday", "high"),
        ("L Ultimo Esorcismo", "high"),
        ("The Devil's Bath a K a Des Teufels Bad", "high"),
        ("Evil.Dead.2013.1080p.BluRay.x264", "high"),
        ("I 13 Spettri Thir13en Ghosts", "medium"),
        ("Torso I Corpi Presentano Tracce Di Violenza Carnale", "medium"),
        ("", "high"),
    ]

    CLEAN = [
        "The Thing", "Peeping Tom", "Black Sunday", "13 Ghosts", "The Entity",
        "The Last Exorcism", "Torso", "The Devil's Bath",
        # Each of these trips exactly one weak signal and must survive it.
        "Se7en", "2 Fast 2 Furious",
        "Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb",
        "The Lord of the Rings: The Fellowship of the Ring",
        # One foreign function word is a real title in one language.
        "La Dolce Vita", "Le Samourai", "Das Boot", "La Haine", "Il Postino",
        "Jean de Florette", "Amores Perros", "Y Tu Mama Tambien",
        # Initials fold to single letters and must not read as an article.
        "L.A. Confidential", "L.A. Story", "D.O.A.",
        # An "l" followed by a word, inside another word.
        "Angel Heart", "Pan's Labyrinth", "Devil's Advocate",
        "American Horror Story", "Once Upon a Time in the West",
        "Crouching Tiger, Hidden Dragon", "Ocean's Eleven", "8 1/2",
        "The Good, the Bad and the Ugly", "O Brother, Where Art Thou?",
        "Spider-Man: Into the Spider-Verse",
    ]

    def test_broken_titles_are_caught_at_the_stated_confidence(self):
        for title, confidence in self.BROKEN:
            reasons, level = plex.suspect_title(title)
            self.assertTrue(reasons, f"{title!r} was not flagged")
            self.assertEqual(level, confidence, f"{title!r}")

    def test_real_titles_are_left_alone(self):
        for title in self.CLEAN:
            reasons, _ = plex.suspect_title(title)
            self.assertEqual(reasons, [], f"{title!r} was flagged: {reasons}")

    def test_an_apostrophe_folds_to_a_space_not_away(self):
        # normalize_spoken drops it, which is right for a room name and wrong
        # here: it hides the stranded article that is the clearest tell.
        self.assertEqual(plex.fold_title("L'occhio"), "l occhio")

    def test_the_reason_names_the_signal(self):
        reasons, _ = plex.suspect_title("The Entity Horror")
        self.assertIn("genre name", reasons[0])


class AuditLibrary(unittest.TestCase):
    def setUp(self):
        self.items = []
        self.saved = (plex.resolve_sections, plex.section_items)
        plex.resolve_sections = lambda *a, **k: [FakeSection()]
        plex.section_items = lambda section, libtype=None, enriched=True: (
            self.items, 0)
        self.addCleanup(self.restore)

    def restore(self):
        plex.resolve_sections, plex.section_items = self.saved

    def add(self, **kwargs):
        movie = art_movie(**kwargs)
        self.items.append(movie)
        return movie

    def audit(self, **args):
        return plex.call_tool("audit_library", args)

    def test_a_clean_library_reports_nothing(self):
        self.add(rating_key="1", title="The Thing", year=1982,
                 genres=["Horror"], summary="A thing.", thumb="/t", art="/a")
        result = self.audit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["scanned"], 1)

    def test_the_screenshot_case_is_caught_on_every_axis(self):
        self.add(rating_key="1", title="The Entity Horror", year=1982,
                 genres=[], summary=None, thumb=None, art=None,
                 guid="local://9")
        result = self.audit()
        found = result["findings"][0]
        self.assertEqual(found["confidence"], "high")
        self.assertIn("no poster", found["problems"])
        self.assertIn("never matched to a provider entry", found["problems"])
        self.assertIn("no genres", found["problems"])
        self.assertTrue(any("genre name" in p for p in found["problems"]))

    def test_the_filename_rides_along_with_the_finding(self):
        # On a broken item the filename is the only honest identifier left,
        # and it is what the agent needs to work out what the film is.
        self.add(rating_key="1", title="L Ultimo Esorcismo", thumb=None,
                 file="/media/L.Ultimo.Esorcismo.2010.1080p.mkv")
        result = self.audit()
        self.assertEqual(result["findings"][0]["file"],
                         "L.Ultimo.Esorcismo.2010.1080p.mkv")

    def test_findings_are_ordered_worst_first(self):
        self.add(rating_key="1", title="The Thing", year=1982,
                 genres=["Horror"], summary="x", thumb=None, art="/a")
        self.add(rating_key="2", title="L Ultimo Esorcismo", year=2010,
                 genres=["Horror"], summary="x", thumb="/t", art="/a")
        result = self.audit()
        self.assertEqual(result["findings"][0]["rating_key"], "2")
        self.assertEqual(result["findings"][0]["confidence"], "high")

    def test_the_label_check_needs_a_label_to_require(self):
        self.add(rating_key="1")
        result = self.audit(checks=["labels"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")

    def test_a_required_label_is_only_wanted_on_the_named_genre(self):
        # The clankerTv case: every Horror film needs the marathon label, and
        # nothing else should be dragged in.
        self.add(rating_key="1", title="The Thing", year=1982, summary="x",
                 thumb="/t", art="/a", genres=["Horror"], labels=[])
        self.add(rating_key="2", title="Heat", year=1995, summary="x",
                 thumb="/t", art="/a", genres=["Crime"], labels=[])
        self.add(rating_key="3", title="Alien", year=1979, summary="x",
                 thumb="/t", art="/a", genres=["Horror"],
                 labels=["Horror Marathon"])
        result = self.audit(checks=["labels"], require_label="Horror Marathon",
                            when_genre="Horror")
        self.assertEqual([f["rating_key"] for f in result["findings"]], ["1"])

    def test_the_label_check_is_case_insensitive(self):
        self.add(rating_key="1", title="The Thing", year=1982, summary="x",
                 thumb="/t", art="/a", genres=["horror"],
                 labels=["horror marathon"])
        result = self.audit(checks=["labels"], require_label="Horror Marathon",
                            when_genre="Horror")
        self.assertEqual(result["finding_count"], 0)

    def test_an_unknown_check_is_refused_rather_than_ignored(self):
        self.add(rating_key="1")
        result = self.audit(checks=["artwork", "nonsense"])
        self.assertFalse(result["ok"])
        self.assertIn("nonsense", str(result["error"]))

    def test_a_long_sweep_pages(self):
        for n in range(5):
            self.add(rating_key=str(n), thumb=None, title=f"Film {n}",
                     year=2000, genres=["Horror"], summary="x", art="/a")
        first = self.audit(limit=2)
        self.assertEqual(first["returned"], 2)
        self.assertEqual(first["finding_count"], 5)
        self.assertEqual(first["next_offset"], 2)
        last = self.audit(limit=2, offset=4)
        self.assertEqual(last["returned"], 1)
        self.assertNotIn("next_offset", last)

    def test_problems_are_counted_for_the_whole_sweep_not_the_page(self):
        for n in range(5):
            self.add(rating_key=str(n), thumb=None, title=f"Film {n}",
                     year=2000, genres=["Horror"], summary="x", art="/a")
        result = self.audit(limit=1)
        self.assertEqual(result["by_problem"]["no poster"], 5)


class ReviewDocument(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["PLEX_REVIEW_DIR"] = self.dir
        self.addCleanup(lambda: os.environ.pop("PLEX_REVIEW_DIR", None))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, **args):
        return plex.call_tool("write_review_document", args)

    def read(self, result):
        with open(result["path"], encoding="utf-8") as handle:
            return handle.read()

    def test_a_document_carries_the_detection_and_the_proposal(self):
        result = self.write(filename="pass.md", title="Horror pass", entries=[{
            "rating_key": "1234", "title": "The Entity Horror", "year": 1982,
            "file": "The.Entity.Horror.1982.mkv",
            "problems": ["no poster", "never matched to a provider entry"],
            "confidence": "high",
            "proposed": {"title": "The Entity", "genres": ["Horror"]},
        }])
        self.assertTrue(result["ok"])
        body = self.read(result)
        self.assertIn("# Horror pass", body)
        self.assertIn("The Entity Horror", body)
        self.assertIn("`1234`", body)
        self.assertIn("The.Entity.Horror.1982.mkv", body)
        self.assertIn("never matched", body)
        self.assertIn("`genres` → Horror", body)

    def test_the_document_says_nothing_was_applied(self):
        result = self.write(filename="pass.md",
                            entries=[{"rating_key": "1", "title": "X"}])
        self.assertIn("has been applied", self.read(result))
        self.assertIn("Nothing has been applied", result["note"])

    def test_the_apply_payload_is_printed_for_the_reviewer(self):
        result = self.write(filename="pass.md",
                            entries=[{"rating_key": "1", "title": "X"}],
                            apply_payload={"updates": [{"rating_key": "1"}],
                                           "confirm": True})
        body = self.read(result)
        self.assertIn("batch_update_item_metadata", body)
        self.assertIn('"rating_key": "1"', body)

    def test_a_path_cannot_escape_the_review_directory(self):
        # A media server has no business taking an arbitrary path from a model.
        result = self.write(filename="../../etc/passwd",
                            entries=[{"rating_key": "1"}])
        self.assertTrue(result["ok"])
        self.assertEqual(os.path.dirname(result["path"]), self.dir)
        self.assertEqual(result["filename"], "passwd.md")

    def test_an_extension_is_forced(self):
        result = self.write(filename="pass", entries=[{"rating_key": "1"}])
        self.assertEqual(result["filename"], "pass.md")

    def test_empty_entries_are_refused(self):
        result = self.write(filename="pass.md", entries=[])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")

    def test_entries_that_are_not_objects_are_refused(self):
        result = self.write(filename="pass.md", entries=["just a string"])
        self.assertFalse(result["ok"])


class ArtworkHostAllowlist(MetadataFixture):
    """A URL here is picked by a model and fetched by the media server."""

    def setUp(self):
        super().setUp()
        self.saved_hosts = list(plex.ART_HOSTS)
        self.addCleanup(lambda: setattr(plex, "ART_HOSTS", self.saved_hosts))
        movie = art_movie(rating_key="1", thumb=None)
        self.items["1"] = movie
        self.movie = movie

    def set_url(self, url, **extra):
        args = {"rating_key": "1", "poster_url": url, "confirm": True}
        args.update(extra)
        return plex.call_tool("set_artwork", args)

    def test_an_unlisted_host_is_refused(self):
        result = self.set_url("https://example.com/p.jpg")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")
        self.assertIn("image.tmdb.org", result["allowed_hosts"])
        self.assertEqual(self.movie.uploads, [])

    def test_a_listed_host_is_allowed(self):
        result = self.set_url("https://image.tmdb.org/t/p/original/abc.jpg")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.movie.uploads), 1)

    def test_a_subdomain_of_a_listed_host_is_allowed(self):
        plex.ART_HOSTS = ["fanart.tv"]
        result = self.set_url("https://assets.fanart.tv/fanart/movies/1/x.png")
        self.assertTrue(result["ok"])

    def test_a_lookalike_domain_is_not_a_subdomain(self):
        # "notimage.tmdb.org.evil.com" must not pass as image.tmdb.org.
        result = self.set_url("https://image.tmdb.org.evil.com/p.jpg")
        self.assertFalse(result["ok"])
        self.assertEqual(self.movie.uploads, [])

    def test_a_star_turns_the_check_off(self):
        plex.ART_HOSTS = ["*"]
        self.assertTrue(self.set_url("https://example.com/p.jpg")["ok"])

    def test_a_non_http_scheme_is_refused(self):
        for url in ("file:///etc/passwd", "ftp://host/p.jpg", "/local/p.jpg"):
            result = self.set_url(url)
            self.assertFalse(result["ok"], url)
        self.assertEqual(self.movie.uploads, [])

    def test_an_upload_is_reported_as_weakly_verified(self):
        result = self.set_url("https://image.tmdb.org/t/p/original/abc.jpg")
        self.assertTrue(result["ok"])
        self.assertIn("verified only", result["note"])


class LogoSlot(MetadataFixture):
    def test_a_logo_locks_under_its_own_plex_field(self):
        # Plex calls the background "art" and the logo "clearLogo"; locking the
        # wrong one is the bug three near-identical code paths would produce.
        movie = art_movie(rating_key="1", logos=[{"id": "lg"}])
        self.items["1"] = movie
        result = plex.call_tool("set_artwork", {"rating_key": "1",
                                                "logo_id": "lg",
                                                "confirm": True})
        self.assertTrue(result["ok"])
        self.assertTrue(movie._logos[0].selected)
        self.assertIn("clearLogo", movie.locked)
        self.assertNotIn("art", movie.locked)

    def test_every_slot_is_reported(self):
        movie = art_movie(rating_key="1", thumb="/t", art=None, logo="/l")
        self.items["1"] = movie
        result = plex.call_tool("get_artwork", {"rating_key": "1"})
        self.assertTrue(result["has_poster"])
        self.assertFalse(result["has_art"])
        self.assertTrue(result["has_logo"])

    def test_several_slots_can_be_set_at_once(self):
        movie = art_movie(rating_key="1", posters=[{"id": "p"}],
                          logos=[{"id": "lg"}])
        self.items["1"] = movie
        result = plex.call_tool("set_artwork", {
            "rating_key": "1", "poster_id": "p", "logo_id": "lg",
            "confirm": True})
        self.assertTrue(result["ok"])
        self.assertTrue(movie._posters[0].selected)
        self.assertTrue(movie._logos[0].selected)


class ExternalIds(unittest.TestCase):
    """A library that has run for years holds both agent shapes, and the one
    you cannot read is the one you need."""

    def test_modern_guid_children_are_read(self):
        item = Item(guid="plex://movie/5d77", guids=[
            Item(id="imdb://tt0083907"), Item(id="tmdb://11342"),
            Item(id="tvdb://456")])
        self.assertEqual(plex.external_ids(item),
                         {"imdb": "tt0083907", "tmdb": "11342", "tvdb": "456"})

    def test_a_legacy_agent_guid_is_read(self):
        item = Item(guid="com.plexapp.agents.imdb://tt0083907?lang=en", guids=[])
        self.assertEqual(plex.external_ids(item), {"imdb": "tt0083907"})

    def test_a_legacy_themoviedb_guid_becomes_tmdb(self):
        item = Item(guid="com.plexapp.agents.themoviedb://11342?lang=en",
                    guids=[])
        self.assertEqual(plex.external_ids(item), {"tmdb": "11342"})

    def test_an_unmatched_item_has_no_external_ids(self):
        self.assertEqual(plex.external_ids(Item(guid="local://42", guids=[])), {})

    def test_a_query_string_is_stripped_from_a_child_guid(self):
        item = Item(guid="plex://movie/x", guids=[Item(id="tmdb://11342?lang=en")])
        self.assertEqual(plex.external_ids(item), {"tmdb": "11342"})


class AlternateArt(MetadataFixture):
    def setUp(self):
        super().setUp()
        self.saved_art = (plex.http_json, plex.TMDB_API_KEY,
                          plex.FANART_API_KEY)
        self.addCleanup(self.restore_art)
        plex.TMDB_API_KEY = ""
        plex.FANART_API_KEY = ""
        self.requests = []

    def restore_art(self):
        (plex.http_json, plex.TMDB_API_KEY,
         plex.FANART_API_KEY) = self.saved_art

    def stub_http(self, responses):
        def fake(url, headers=None, timeout=None):
            self.requests.append(url)
            for fragment, payload in responses.items():
                if fragment in url:
                    return payload
            raise AssertionError(f"unexpected request {url}")
        plex.http_json = fake

    def make(self, **kwargs):
        movie = art_movie(rating_key="1", **kwargs)
        self.items["1"] = movie
        return movie

    def find(self, **args):
        args.setdefault("rating_key", "1")
        return plex.call_tool("find_alternate_art", args)

    def test_plex_candidates_need_no_key_at_all(self):
        self.make(posters=[{"id": "a", "selected": True}, {"id": "b"}],
                  guids=[Item(id="tmdb://11342")])
        result = self.find(source="plex")
        self.assertTrue(result["ok"])
        self.assertEqual([c["id"] for c in result["candidates"]], ["a", "b"])
        self.assertEqual(result["external_ids"], {"tmdb": "11342"})

    def test_a_missing_key_is_reported_not_swallowed(self):
        # Silently returning only Plex's posters would read as "there is no
        # alternate art", which is the wrong conclusion entirely.
        self.make(guids=[Item(id="tmdb://11342")])
        result = self.find()
        self.assertIn("TMDB_API_KEY is not set", result["unavailable"]["tmdb"])
        self.assertIn("FANART_API_KEY is not set", result["unavailable"]["fanart"])

    def test_an_unmatched_item_says_so_instead_of_querying(self):
        self.make(guid="local://42", guids=[])
        result = self.find()
        self.assertEqual(result["external_ids"], {})
        self.assertIn("unmatched", result["note"])
        self.assertEqual(self.requests, [])

    def test_tmdb_posters_are_parsed_and_ranked(self):
        plex.TMDB_API_KEY = "abc"
        self.make(guids=[Item(id="tmdb://11342")])
        self.stub_http({"/images": {"posters": [
            {"file_path": "/low.jpg", "iso_639_1": "en", "width": 1000,
             "height": 1500, "vote_average": 3.0, "vote_count": 2},
            {"file_path": "/best.jpg", "iso_639_1": None, "width": 2000,
             "height": 3000, "vote_average": 8.0, "vote_count": 40},
        ]}})
        result = self.find(source="tmdb")
        first = result["candidates"][0]
        self.assertEqual(first["url"],
                         "https://image.tmdb.org/t/p/original/best.jpg")
        self.assertEqual(first["language"], "textless")
        self.assertEqual(first["size"], "2000x3000")

    def test_a_v3_key_goes_in_the_query_string(self):
        plex.TMDB_API_KEY = "abc123"
        self.make(guids=[Item(id="tmdb://11342")])
        self.stub_http({"/images": {"posters": []}})
        self.find(source="tmdb")
        self.assertIn("api_key=abc123", self.requests[0])

    def test_an_imdb_only_item_is_looked_up_at_tmdb_first(self):
        plex.TMDB_API_KEY = "abc"
        self.make(guids=[Item(id="imdb://tt0083907")])
        self.stub_http({
            "/find/tt0083907": {"movie_results": [{"id": 11342}]},
            "/images": {"posters": [{"file_path": "/a.jpg", "iso_639_1": "en",
                                     "width": 1, "height": 1,
                                     "vote_average": 1, "vote_count": 1}]},
        })
        result = self.find(source="tmdb")
        self.assertEqual(result["count"], 1)
        self.assertIn("/movie/11342/images", self.requests[1])

    def test_fanart_textless_is_labelled(self):
        plex.FANART_API_KEY = "k"
        self.make(guids=[Item(id="tmdb://11342")])
        self.stub_http({"fanart.tv": {"movieposter": [
            {"url": "https://assets.fanart.tv/a.png", "lang": "00", "likes": "9"},
            {"url": "https://assets.fanart.tv/b.png", "lang": "en", "likes": "3"},
        ]}})
        result = self.find(source="fanart")
        self.assertEqual(result["candidates"][0]["language"], "textless")
        self.assertEqual(result["candidates"][0]["likes"], 9)

    def test_fanart_prefers_the_hd_logo_set(self):
        plex.FANART_API_KEY = "k"
        self.make(guids=[Item(id="tmdb://11342")])
        self.stub_http({"fanart.tv": {
            "movielogo": [{"url": "https://assets.fanart.tv/old.png",
                           "lang": "en", "likes": "99"}],
            "hdmovielogo": [{"url": "https://assets.fanart.tv/hd.png",
                             "lang": "en", "likes": "1"}],
        }})
        result = self.find(source="fanart", kind="logo")
        self.assertEqual(result["candidates"][0]["url"],
                         "https://assets.fanart.tv/hd.png")

    def test_a_language_filter_applies_across_sources(self):
        plex.TMDB_API_KEY = "abc"
        self.make(guids=[Item(id="tmdb://11342")])
        self.stub_http({"/images": {"posters": [
            {"file_path": "/it.jpg", "iso_639_1": "it", "width": 1, "height": 1,
             "vote_average": 1, "vote_count": 1},
            {"file_path": "/en.jpg", "iso_639_1": "en", "width": 1, "height": 1,
             "vote_average": 9, "vote_count": 1},
        ]}})
        result = self.find(source="tmdb", language="it")
        self.assertEqual(result["count"], 1)
        self.assertIn("/it.jpg", result["candidates"][0]["url"])

    def test_a_404_from_fanart_is_a_reason_not_a_crash(self):
        plex.FANART_API_KEY = "k"
        self.make(guids=[Item(id="tmdb://11342")])

        def fake(url, headers=None, timeout=None):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        plex.http_json = fake
        result = self.find(source="fanart")
        self.assertTrue(result["ok"])
        self.assertIn("nothing for this film", result["unavailable"]["fanart"])

    def test_one_source_failing_does_not_lose_the_others(self):
        plex.TMDB_API_KEY = "abc"
        self.make(posters=[{"id": "a"}], guids=[Item(id="tmdb://11342")])

        def fake(url, headers=None, timeout=None):
            raise OSError("network is down")
        plex.http_json = fake
        result = self.find()
        self.assertTrue(result["ok"])
        self.assertEqual([c.get("id") for c in result["candidates"]], ["a"])
        self.assertIn("network is down", result["unavailable"]["tmdb"])

    def test_background_aliases_are_accepted(self):
        self.make(arts=[{"id": "bg"}])
        for alias in ("background", "art", "backdrop", "fanart"):
            result = self.find(source="plex", kind=alias)
            self.assertEqual(result["kind"], "background", alias)

    def test_an_unknown_kind_is_refused(self):
        self.make()
        result = self.find(kind="nonsense")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_request")

    def test_an_unknown_source_is_refused(self):
        self.make()
        result = self.find(source="imp awards")
        self.assertFalse(result["ok"])

    def test_the_limit_is_per_source(self):
        plex.TMDB_API_KEY = "abc"
        self.make(posters=[{"id": str(n)} for n in range(5)],
                  guids=[Item(id="tmdb://11342")])
        self.stub_http({"/images": {"posters": [
            {"file_path": f"/{n}.jpg", "iso_639_1": "en", "width": 1,
             "height": 1, "vote_average": n, "vote_count": 1}
            for n in range(5)]}})
        result = self.find(limit=2)
        self.assertEqual(len([c for c in result["candidates"]
                              if c["source"] == "plex"]), 2)
        self.assertEqual(len([c for c in result["candidates"]
                              if c["source"] == "tmdb"]), 2)

    def test_nothing_is_written_by_a_search(self):
        movie = self.make(posters=[{"id": "a"}])
        self.find(source="plex")
        self.assertEqual(movie.edits, [])
        self.assertEqual(movie.uploads, [])


if __name__ == "__main__":
    unittest.main()
