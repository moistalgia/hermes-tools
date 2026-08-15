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


if __name__ == "__main__":
    unittest.main()
