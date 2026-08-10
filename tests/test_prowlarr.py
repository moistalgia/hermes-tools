"""Title parsing, magnet reconstruction, and the difference between the four
kinds of empty.

`prowlarr-mcp` has no write path and no read-back problem, so what it can get
wrong is narrower than the other servers here — and correspondingly quieter.
Every failure in this file is a confident wrong answer rather than an error:

  * a cam rip presented as the best result,
  * a `.torrent` URL handed over in the magnet slot, where it looks right until
    something three steps downstream tries to use it,
  * "nothing found" reported for a title that exists, because both indexers
    were failing and an empty list looks identical either way.

The fake Prowlarr is deliberately dumb. It does no matching and no ranking,
because those are exactly the parts under test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

from mcpkit import ToolError  # noqa: E402


def load(**env):
    settings = {"PROWLARR_URL": "http://prowlarr.test:9696",
                "PROWLARR_API_KEY": "test-key",
                "PROWLARR_FETCH_PREFIX": "!fetch"}
    settings.update(env)
    return support.load("prowlarr_server", "prowlarr-mcp/prowlarr_mcp_server.py", settings)


def release(title, **overrides):
    """One Prowlarr search result, with the fields the server actually reads."""
    row = {
        "title": title,
        "indexer": "TestTracker",
        "indexerId": 1,
        "protocol": "torrent",
        "seeders": 10,
        "leechers": 1,
        "size": 8 * 1024 ** 3,
        "publishDate": "2026-08-01T12:00:00Z",
        "infoHash": "a" * 40,
        "magnetUrl": None,
        "downloadUrl": "http://prowlarr.test:9696/download/1",
    }
    row.update(overrides)
    return row


def indexer(name, ident=1, enable=True, protocol="torrent"):
    return {"id": ident, "name": name, "enable": enable, "protocol": protocol,
            "privacy": "public",
            "capabilities": {"categories": [{"id": 2000, "name": "Movies"},
                                            {"id": 5000, "name": "TV"}]}}


class FakeProwlarr:
    """Just enough Prowlarr. Records what was asked, answers with fixtures."""

    def __init__(self, indexers=None, releases=None, broken=None):
        self.indexers = indexers if indexers is not None else [indexer("TestTracker")]
        self.releases = releases if releases is not None else []
        self.broken = broken or []
        self.searches = []

    def install(self, module):
        module.api = self.api
        return self

    def api(self, path, params=None, timeout=None):
        if path == "system/status":
            return {"version": "1.30.2.4939", "appName": "Prowlarr"}
        if path == "indexer":
            return self.indexers
        if path == "indexerstatus":
            return self.broken
        if path == "search":
            self.searches.append(params or {})
            return self.releases
        raise AssertionError(f"FakeProwlarr got an unexpected path: {path!r}")


class TestReadingTitles(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_resolution_source_and_codec_come_out_of_the_name(self):
        parsed = self.server.read_title("Some.Film.2019.1080p.BluRay.x265-GROUP")
        self.assertEqual(parsed["resolution"], 1080)
        self.assertEqual(parsed["source"], "bluray")
        self.assertEqual(parsed["codec"], "x265")
        self.assertFalse(parsed["cam"])

    def test_remux_is_reported_as_remux_not_bluray(self):
        # Both words are in the title. The more specific one is the useful one.
        parsed = self.server.read_title("Some.Film.2019.2160p.BluRay.REMUX.HDR.HEVC")
        self.assertEqual(parsed["source"], "remux")
        self.assertEqual(parsed["resolution"], 2160)
        self.assertTrue(parsed["hdr"])

    def test_a_streaming_service_tag_counts_as_web_dl(self):
        parsed = self.server.read_title("Some.Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb")
        self.assertEqual(parsed["source"], "web-dl")
        self.assertEqual(parsed["codec"], "x264")

    def test_an_unambiguous_cam_is_flagged_however_it_is_labelled(self):
        for title in ("Film.2026.HDCAM.1080p.x264",
                      "Film.2026.TELESYNC.720p",
                      "Film.2026.DVDSCR.XviD"):
            with self.subTest(title=title):
                self.assertTrue(self.server.read_title(title)["cam"])

    def test_a_bare_ts_only_counts_when_nothing_claims_a_real_resolution(self):
        # "TS" is telesync and is also three letters of a release group name.
        # Flagging a good 1080p release as a cam rip would bury it.
        self.assertTrue(self.server.read_title("Film.2026.TS.XviD-nogroup")["cam"])
        self.assertFalse(
            self.server.read_title("Film.2026.1080p.WEB-DL.x264-TS")["cam"])


class TestMagnets(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_a_real_magnet_is_passed_through_untouched(self):
        magnet = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=Thing"
        got, note = self.server.magnet_for(release("Thing", magnetUrl=magnet))
        self.assertEqual(got, magnet)
        self.assertIsNone(note)

    def test_a_missing_magnet_is_rebuilt_from_the_info_hash(self):
        got, note = self.server.magnet_for(release("Some Film 2019", infoHash="C" * 40))
        self.assertTrue(got.startswith("magnet:?xt=urn:btih:" + "c" * 40))
        self.assertIn("dn=Some%20Film%202019", got)
        self.assertIn("info hash", note)

    def test_a_torrent_url_is_never_returned_as_a_magnet(self):
        # The failure this prevents: something that looks like a magnet slot
        # being filled with an http link, discovered three steps downstream.
        got, note = self.server.magnet_for(
            release("Thing", infoHash=None, magnetUrl=None))
        self.assertIsNone(got)
        self.assertIn(".torrent", note)

    def test_a_malformed_info_hash_is_not_dressed_up_as_one(self):
        got, _note = self.server.magnet_for(release("Thing", infoHash="not-a-hash"))
        self.assertIsNone(got)


class TestQueryBuilding(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_season_and_episode_become_scene_notation(self):
        self.assertEqual(self.server.build_query("Some Show", season=1, episode=2),
                         "Some Show S01E02")
        self.assertEqual(self.server.build_query("Some Show", season=12),
                         "Some Show S12")

    def test_an_episode_without_a_season_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.server.build_query("Some Show", episode=4)
        self.assertIn("season", str(caught.exception))

    def test_a_year_is_dropped_for_an_episode_search(self):
        # The year in a TV title is the series year and is almost never in the
        # release name, so including it matches nothing at all.
        self.assertEqual(self.server.build_query("Some Show", year=2019, season=1),
                         "Some Show S01")
        self.assertEqual(self.server.build_query("Some Film", year=2019),
                         "Some Film 2019")

    def test_an_empty_query_is_refused_rather_than_sent(self):
        with self.assertRaises(ToolError):
            self.server.build_query("   ")


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_results_are_ranked_with_cams_last(self):
        FakeProwlarr(releases=[
            release("Film.2026.HDCAM.1080p.x264", seeders=900, infoHash="1" * 40),
            release("Film.2026.720p.WEB-DL.x264", seeders=20, infoHash="2" * 40),
            release("Film.2026.1080p.BluRay.x264", seeders=5, infoHash="3" * 40),
        ]).install(self.server)

        result = self.server.search(query="Film", year=2026)
        titles = [r["title"] for r in result["results"]]
        self.assertTrue(titles[0].startswith("Film.2026.1080p"))
        self.assertTrue(titles[-1].startswith("Film.2026.HDCAM"),
                        msg="a cam rip with 900 seeders must still rank last")
        self.assertEqual([r["n"] for r in result["results"]], [1, 2, 3])

    def test_sort_by_seeders_ignores_quality_when_asked(self):
        FakeProwlarr(releases=[
            release("Film.2026.1080p.BluRay.x264", seeders=5, infoHash="3" * 40),
            release("Film.2026.720p.WEB-DL.x264", seeders=20, infoHash="2" * 40),
        ]).install(self.server)
        result = self.server.search(query="Film", sort="seeders")
        self.assertEqual(result["results"][0]["seeders"], 20)

    def test_the_same_release_on_three_indexers_is_one_row(self):
        FakeProwlarr(releases=[
            release("Film.2026.1080p.BluRay.x264", indexer="A", seeders=3),
            release("Film.2026.1080p.BluRay.x264", indexer="B", seeders=40),
            release("Film.2026.1080p.BluRay.x264", indexer="C", seeders=8),
        ]).install(self.server)

        result = self.server.search(query="Film")
        self.assertEqual(len(result["results"]), 1)
        row = result["results"][0]
        self.assertEqual(row["indexer"], "B", msg="the healthiest copy is the one kept")
        self.assertEqual(row["also_on"], ["A", "C"])

    def test_low_seeder_releases_are_dropped_but_usenet_is_not(self):
        FakeProwlarr(releases=[
            release("Film.2026.1080p.WEB-DL.x264", seeders=0, infoHash="1" * 40),
            release("Film.2026.1080p.BluRay.x264", seeders=9, infoHash="2" * 40),
            # Usenet has no seeders at all. Reading `None` as zero would filter
            # away every usenet result the moment min_seeders rose above nothing.
            release("Film.2026.2160p.WEB-DL.x265", seeders=None, protocol="usenet",
                    infoHash="3" * 40, magnetUrl="magnet:?xt=urn:btih:" + "3" * 40),
        ]).install(self.server)

        result = self.server.search(query="Film", min_seeders=1)
        protocols = {r["protocol"] for r in result["results"]}
        self.assertEqual(len(result["results"]), 2)
        self.assertIn("usenet", protocols)

    def test_every_result_carries_a_ready_to_send_fetch_line(self):
        FakeProwlarr(releases=[release("Film.2026.1080p.BluRay.x264")]).install(self.server)
        row = self.server.search(query="Film")["results"][0]
        self.assertTrue(row["fetch_command"].startswith("!fetch magnet:?xt=urn:btih:"))

    def test_the_fetch_line_is_omitted_when_no_prefix_is_configured(self):
        server = load(PROWLARR_FETCH_PREFIX="")
        FakeProwlarr(releases=[release("Film.2026.1080p.BluRay.x264")]).install(server)
        row = server.search(query="Film")["results"][0]
        self.assertNotIn("fetch_command", row)

    def test_limit_is_respected(self):
        fake = FakeProwlarr(releases=[
            release(f"Film.2026.1080p.BluRay.x264-G{n}", infoHash=f"{n:040d}")
            for n in range(20)])
        fake.install(self.server)
        self.assertEqual(len(self.server.search(query="Film", limit=3)["results"]), 3)

    def test_a_kind_narrows_the_categories_prowlarr_is_given(self):
        fake = FakeProwlarr(releases=[release("Some.Show.S01E01.1080p.WEB-DL.x264")])
        fake.install(self.server)
        self.server.search(query="Some Show", kind="tv", season=1, episode=1)
        sent = fake.searches[-1]
        self.assertEqual(sent["categories"], [5000])
        self.assertEqual(sent["query"], "Some Show S01E01")

    def test_kind_any_sends_no_category_filter_at_all(self):
        fake = FakeProwlarr(releases=[release("Film.2026.1080p.BluRay.x264")])
        fake.install(self.server)
        self.server.search(query="Film", kind="any")
        self.assertNotIn("categories", fake.searches[-1])


class TestIndexerResolution(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_a_name_resolves_by_prefix(self):
        FakeProwlarr(indexers=[indexer("1337x", 1), indexer("YTS", 2)]).install(self.server)
        self.assertEqual(self.server.resolve_indexer("yts")["id"], 2)

    def test_an_unknown_name_lists_the_ones_that_exist(self):
        FakeProwlarr(indexers=[indexer("1337x", 1), indexer("YTS", 2)]).install(self.server)
        with self.assertRaises(ToolError) as caught:
            self.server.resolve_indexer("piratebay")
        self.assertEqual(sorted(caught.exception.extra["enabled_indexers"]), ["1337x", "YTS"])

    def test_searching_a_disabled_indexer_says_so_instead_of_returning_nothing(self):
        FakeProwlarr(indexers=[indexer("1337x", 1, enable=False)]).install(self.server)
        with self.assertRaises(ToolError) as caught:
            self.server.search(query="Film", indexer="1337x")
        self.assertIn("disabled", str(caught.exception))


class TestEmptyIsADiagnosis(unittest.TestCase):
    """Four ways to get zero rows, needing four different responses."""

    def setUp(self):
        self.server = load()

    def test_nothing_matched_is_an_ordinary_answer(self):
        FakeProwlarr(releases=[]).install(self.server)
        result = self.server.search(query="Film That Does Not Exist")
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [])
        self.assertIn("Nothing found", result["summary"])

    def test_everything_filtered_out_says_it_was_filtered(self):
        FakeProwlarr(releases=[
            release("Film.2026.1080p.BluRay.x264", seeders=0)]).install(self.server)
        result = self.server.search(query="Film", min_seeders=5)
        self.assertTrue(result["ok"])
        self.assertIn("min_seeders", result["summary"])
        self.assertEqual(result["total_before_ranking"], 1)

    def test_every_indexer_failing_is_not_reported_as_nothing_found(self):
        # The one that matters. An agent told "nothing found" tries another
        # title; an agent told the indexer is down stops and reports it.
        FakeProwlarr(
            indexers=[indexer("1337x", 1)],
            releases=[],
            broken=[{"indexerId": 1, "mostRecentFailure": "2026-08-10T09:00:00Z",
                     "disabledTill": "2026-08-10T10:00:00Z"}],
        ).install(self.server)

        result = self.server.search(query="Film")
        self.assertFalse(result["ok"])
        self.assertIn("1337x", result["error"])
        self.assertIn("every indexer searched is failing", result["error"])

    def test_one_of_two_failing_warns_that_the_answer_may_be_incomplete(self):
        FakeProwlarr(
            indexers=[indexer("1337x", 1), indexer("YTS", 2)],
            releases=[],
            broken=[{"indexerId": 1, "mostRecentFailure": "2026-08-10T09:00:00Z"}],
        ).install(self.server)

        result = self.server.search(query="Film")
        self.assertTrue(result["ok"])
        self.assertIn("1337x", result["summary"])
        self.assertIn("incomplete", result["summary"])

    def test_no_enabled_indexers_at_all_is_a_failure_not_an_empty_list(self):
        FakeProwlarr(indexers=[indexer("1337x", 1, enable=False)],
                     releases=[]).install(self.server)
        result = self.server.search(query="Film")
        self.assertFalse(result["ok"])
        self.assertIn("asked nobody", result["error"])


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_a_healthy_instance_reports_ok(self):
        FakeProwlarr(indexers=[indexer("1337x", 1), indexer("YTS", 2)]).install(self.server)
        result = self.server.prowlarr_status()
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["indexers_enabled"]), ["1337x", "YTS"])

    def test_a_failing_indexer_makes_status_not_ok_and_names_it(self):
        FakeProwlarr(
            indexers=[indexer("1337x", 1)],
            broken=[{"indexerId": 1, "mostRecentFailure": "challenge not solved"}],
        ).install(self.server)
        result = self.server.prowlarr_status()
        self.assertFalse(result["ok"])
        self.assertIn("1337x", result["summary"])
        self.assertIn("challenge not solved", result["indexers_failing"][0])

    def test_no_indexers_configured_is_reported_as_the_problem_it_is(self):
        FakeProwlarr(indexers=[]).install(self.server)
        result = self.server.prowlarr_status()
        self.assertFalse(result["ok"])
        self.assertIn("No indexers", result["error"])

    def test_list_indexers_hands_back_names_to_use_verbatim(self):
        FakeProwlarr(indexers=[indexer("1337x", 1), indexer("YTS", 2)]).install(self.server)
        result = self.server.list_indexers()
        self.assertEqual([r["name"] for r in result["indexers"]], ["1337x", "YTS"])
        self.assertIn("verbatim", result["summary"])


class TestConfiguration(unittest.TestCase):
    def test_a_missing_api_key_is_named_before_anything_is_attempted(self):
        server = load(PROWLARR_API_KEY="")
        with self.assertRaises(ToolError) as caught:
            server.api("system/status")
        self.assertIn("PROWLARR_API_KEY", str(caught.exception))
        self.assertIn("Settings", str(caught.exception))


class TestVendoredProtocolLayer(unittest.TestCase):
    def test_the_copy_of_mcpkit_matches_the_one_at_the_repo_root(self):
        # prowlarr-mcp/ ships its own copy so the directory can be dropped into
        # an MCP folder on its own. A copy that drifts is a server that behaves
        # differently once deployed than it does under test, which is the worst
        # possible place for a difference to live.
        root = os.path.join(support.ROOT, "mcpkit.py")
        vendored = os.path.join(support.ROOT, "prowlarr-mcp", "mcpkit.py")
        with open(root, "r", encoding="utf-8") as fh:
            original = fh.read()
        with open(vendored, "r", encoding="utf-8") as fh:
            copy = fh.read()
        self.assertEqual(original, copy,
                         msg="prowlarr-mcp/mcpkit.py has drifted from mcpkit.py. "
                             "Re-copy the root file over it.")


if __name__ == "__main__":
    unittest.main()
