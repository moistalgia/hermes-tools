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

import base64
import hashlib
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

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
        got, note, url = self.server.magnet_for(release("Thing", magnetUrl=magnet))
        self.assertEqual(got, magnet)
        self.assertIsNone(note)
        self.assertIsNone(url)

    def test_a_missing_magnet_is_rebuilt_from_the_info_hash(self):
        got, note, _url = self.server.magnet_for(release("Some Film 2019", infoHash="C" * 40))
        self.assertTrue(got.startswith("magnet:?xt=urn:btih:" + "c" * 40))
        self.assertIn("dn=Some%20Film%202019", got)
        self.assertIn("info hash", note)

    def test_the_magnet_is_unwrapped_from_prowlarrs_proxy_link(self):
        # The one that matters in practice. Prowlarr Base64s the indexer's own
        # download link into its proxy URL, and for a magnet-based tracker that
        # link IS the magnet — it just never appears in magnetUrl.
        magnet = "magnet:?xt=urn:btih:" + "d" * 40 + "&dn=Thing&tr=udp%3A%2F%2Ftracker%3A80"
        encoded = base64.urlsafe_b64encode(magnet.encode()).decode().rstrip("=")
        got, note, url = self.server.magnet_for(release(
            "Thing", infoHash=None, magnetUrl=None,
            downloadUrl=f"http://prowlarr.test:9696/1/download?apikey=k&link={encoded}&file=Thing"))
        self.assertEqual(got, magnet)
        self.assertIn("download link", note)
        self.assertIsNone(url)

    def test_a_guid_that_is_a_magnet_is_used(self):
        magnet = "magnet:?xt=urn:btih:" + "e" * 40
        got, _note, _url = self.server.magnet_for(release(
            "Thing", infoHash=None, magnetUrl=None, downloadUrl="", guid=magnet))
        self.assertEqual(got, magnet)

    def test_a_torrent_url_is_never_returned_as_a_magnet(self):
        # The failure this prevents: something that looks like a magnet slot
        # being filled with an http link, discovered three steps downstream.
        got, _note, url = self.server.magnet_for(release(
            "Thing", infoHash=None, magnetUrl=None,
            downloadUrl="http://prowlarr.test:9696/1/download?apikey=k&file=Thing"))
        self.assertIsNone(got)
        # Handed back separately, so the resolver can use it and the caller
        # cannot mistake it for a magnet.
        self.assertTrue(url.startswith("http://"))

    def test_a_malformed_info_hash_is_not_dressed_up_as_one(self):
        got, _note, _url = self.server.magnet_for(
            release("Thing", infoHash="not-a-hash", downloadUrl=""))
        self.assertIsNone(got)

    def test_a_proxy_link_that_is_not_base64_is_ignored_rather_than_crashing(self):
        got, _note, url = self.server.magnet_for(release(
            "Thing", infoHash=None, magnetUrl=None,
            downloadUrl="http://prowlarr.test:9696/1/download?link=!!!not-base64!!!"))
        self.assertIsNone(got)
        self.assertTrue(url.startswith("http://"))


def bencode(value):
    """Only what the tests need, and deliberately not the server's own code."""
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(v)
                               for k, v in value.items()) + b"e"
    raise AssertionError(value)


class TestTorrentFiles(unittest.TestCase):
    """The universal fallback: a magnet derived from the .torrent itself."""

    def setUp(self):
        self.server = load()
        self.info = {b"name": b"Some Film 2019 1080p", b"piece length": 262144,
                     b"length": 1234567, b"pieces": b"\x00" * 20}
        self.torrent = bencode({
            b"announce": b"udp://tracker.one:80/announce",
            b"announce-list": [[b"udp://tracker.one:80/announce"],
                               [b"udp://tracker.two:6969/announce"]],
            b"comment": b"irrelevant",
            b"info": self.info,
        })

    def test_the_info_hash_is_the_sha1_of_the_info_dict_as_it_appears_on_the_wire(self):
        # Not of a re-encoded copy. A torrent whose encoder ordered keys
        # differently would hash differently, and the wrong hash produces a
        # magnet that silently finds no peers rather than one that errors.
        expected = hashlib.sha1(bencode(self.info)).hexdigest()
        magnet = self.server.magnet_from_torrent(self.torrent)
        self.assertIn(f"xt=urn:btih:{expected}", magnet)

    def test_the_name_and_trackers_come_along(self):
        magnet = self.server.magnet_from_torrent(self.torrent)
        self.assertIn("dn=Some%20Film%202019%201080p", magnet)
        self.assertIn("tracker.one", magnet)
        self.assertIn("tracker.two", magnet)

    def test_a_tracker_is_not_repeated_when_announce_repeats_it(self):
        self.assertEqual(self.server.magnet_from_torrent(self.torrent).count("tracker.one"), 1)

    def test_a_torrent_with_no_trackers_still_yields_a_magnet(self):
        bare = bencode({b"info": self.info})
        magnet = self.server.magnet_from_torrent(bare)
        self.assertIn("xt=urn:btih:", magnet)
        self.assertNotIn("tr=", magnet)

    def test_something_that_is_not_a_torrent_is_refused_not_guessed_at(self):
        for junk in (b"<html>404</html>", b"", b"d3:foo3:bare"):
            with self.subTest(junk=junk):
                with self.assertRaises(ValueError):
                    self.server.magnet_from_torrent(junk)


class TestResolvingMagnetsDuringSearch(unittest.TestCase):
    """The network half: rows that arrive without a magnet, leaving with one."""

    def setUp(self):
        self.server = load()
        self.fetched = []

    def answer(self, result):
        def fetch(url):
            self.fetched.append(url)
            if isinstance(result, Exception):
                raise result
            return result
        self.server.fetch_download = fetch

    def bare(self, n, **extra):
        """A release with no magnet, no info hash, and a plain proxy link."""
        return release(f"Film.2026.1080p.BluRay.x264-G{n}", infoHash=None, magnetUrl=None,
                       downloadUrl=f"http://prowlarr.test:9696/1/download?apikey=k&file={n}",
                       **extra)

    def test_a_download_link_that_redirects_to_a_magnet_is_followed(self):
        FakeProwlarr(releases=[self.bare(1)]).install(self.server)
        magnet = "magnet:?xt=urn:btih:" + "f" * 40
        self.answer(("magnet", magnet))

        row = self.server.search(query="Film")["results"][0]
        self.assertEqual(row["magnet"], magnet)
        self.assertIn("followed the download link", row["magnet_note"])
        self.assertEqual(row["fetch_command"], f"!fetch {magnet}")

    def test_a_torrent_file_is_turned_into_a_magnet(self):
        FakeProwlarr(releases=[self.bare(1)]).install(self.server)
        info = {b"name": b"Film 2026", b"length": 1, b"piece length": 1, b"pieces": b"\x00" * 20}
        self.answer(("torrent", bencode({b"announce": b"udp://t:80", b"info": info})))

        row = self.server.search(query="Film")["results"][0]
        expected = hashlib.sha1(bencode(info)).hexdigest()
        self.assertIn(f"xt=urn:btih:{expected}", row["magnet"])
        self.assertIn("computed from the .torrent", row["magnet_note"])

    def test_only_the_returned_rows_are_fetched(self):
        # The efficiency claim, and it is not a small one: resolving all 63
        # matches to hand back 10 would be 53 wasted round trips through a
        # solver, each of them seconds long.
        FakeProwlarr(releases=[self.bare(n) for n in range(30)]).install(self.server)
        self.answer(("magnet", "magnet:?xt=urn:btih:" + "f" * 40))

        result = self.server.search(query="Film", limit=4)
        self.assertEqual(len(result["results"]), 4)
        self.assertEqual(len(self.fetched), 4)

    def test_rows_that_already_have_a_magnet_are_not_fetched_at_all(self):
        FakeProwlarr(releases=[release("Film.2026.1080p.BluRay.x264", infoHash="a" * 40)]) \
            .install(self.server)
        self.answer(("magnet", "magnet:?xt=urn:btih:" + "f" * 40))
        self.server.search(query="Film")
        self.assertEqual(self.fetched, [])

    def test_one_release_failing_to_resolve_does_not_fail_the_search(self):
        FakeProwlarr(releases=[self.bare(1)]).install(self.server)
        self.answer(urllib.error.URLError("connection refused"))

        result = self.server.search(query="Film")
        self.assertTrue(result["ok"])
        row = result["results"][0]
        self.assertIsNone(row["magnet"])
        self.assertNotIn("fetch_command", row)
        self.assertIn("could not be read", row["magnet_note"])
        self.assertEqual(result["without_magnet"], 1)

    def test_turning_resolution_off_skips_the_network_and_says_so(self):
        FakeProwlarr(releases=[self.bare(1)]).install(self.server)
        self.answer(("magnet", "magnet:?xt=urn:btih:" + "f" * 40))

        row = self.server.search(query="Film", resolve_magnets=False)["results"][0]
        self.assertEqual(self.fetched, [])
        self.assertIsNone(row["magnet"])
        self.assertIn("resolve_magnets was off", row["magnet_note"])

    def test_the_summary_counts_what_cannot_be_handed_off(self):
        FakeProwlarr(releases=[
            self.bare(1),
            release("Film.2026.720p.WEB-DL.x264", infoHash="b" * 40, seeders=5),
        ]).install(self.server)
        self.answer(urllib.error.URLError("nope"))

        result = self.server.search(query="Film")
        self.assertIn("1 of these has no usable magnet", result["summary"])
        self.assertIn("1 of 2 results carry a magnet", result["note"])


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

    def test_only_the_top_ranked_result_carries_a_ready_to_send_fetch_line(self):
        # Every row already repeats the full magnet in `magnet`; giving every
        # row a second copy in `fetch_command` was pure duplication for the
        # 9 times out of 10 that result is never the one picked.
        FakeProwlarr(releases=[
            release("Film.2026.1080p.BluRay.x264-A", infoHash="a" * 40, seeders=50),
            release("Film.2026.1080p.BluRay.x264-B", infoHash="b" * 40, seeders=10),
        ]).install(self.server)
        rows = self.server.search(query="Film", sort="seeders")["results"]
        self.assertTrue(rows[0]["fetch_command"].startswith("!fetch magnet:?xt=urn:btih:"))
        self.assertNotIn("fetch_command", rows[1])

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

    def test_no_enabled_indexers_at_all_is_refused_before_a_search_is_sent(self):
        fake = FakeProwlarr(indexers=[indexer("1337x", 1, enable=False)], releases=[])
        fake.install(self.server)
        with self.assertRaises(ToolError) as caught:
            self.server.search(query="Film")
        self.assertIn("nothing to search", str(caught.exception))
        self.assertEqual(fake.searches, [], msg="a doomed search must not be sent")


class TestIndexerIdsAreExplicit(unittest.TestCase):
    """Prowlarr has no 'search everything' sentinel, and inventing one 400s.

    It filters its indexers down to the ids the request names, so a value
    matching nothing produces zero indexers and the error 'all selected
    indexers being unavailable' — which describes a completely different
    problem and sends you looking at indexer health instead of at the request.
    """

    def setUp(self):
        self.server = load()

    def test_an_unscoped_search_names_every_enabled_indexer(self):
        fake = FakeProwlarr(
            indexers=[indexer("1337x", 1), indexer("YTS", 2), indexer("Old", 3, enable=False)],
            releases=[])
        fake.install(self.server)
        self.server.search(query="Film")
        self.assertEqual(fake.searches[-1]["indexerIds"], [1, 2],
                         msg="disabled indexers are left out, and -1 is never sent")

    def test_a_scoped_search_names_only_that_indexer(self):
        fake = FakeProwlarr(indexers=[indexer("1337x", 1), indexer("YTS", 2)], releases=[])
        fake.install(self.server)
        self.server.search(query="Film", indexer="YTS")
        self.assertEqual(fake.searches[-1]["indexerIds"], [2])

    def test_prowlarr_refusing_outright_is_translated_not_relayed(self):
        # The raw 400 carries a C# stack trace and blames the indexers. When
        # they really are all backed off, say which and why.
        server = load()

        def refuse(path, params=None, timeout=None):
            if path == "search":
                raise ToolError("Prowlarr returned HTTP 400 for search: Search failed "
                                "due to all selected indexers being unavailable")
            if path == "indexer":
                return [indexer("1337x", 1)]
            if path == "indexerstatus":
                return [{"indexerId": 1, "mostRecentFailure": "challenge not solved"}]
            raise AssertionError(path)

        server.api = refuse
        with self.assertRaises(ToolError) as caught:
            server.search(query="Film")
        self.assertIn("1337x", str(caught.exception))
        self.assertIn("challenge not solved", str(caught.exception))
        self.assertIn("proxy", str(caught.exception))
        self.assertEqual(caught.exception.extra["indexers_asked"], ["1337x"])


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


class TestErrorBodies(unittest.TestCase):
    """Prowlarr answers errors with a message and a C# stack trace."""

    def respond(self, code, body):
        real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", real)

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "error", {}, io.BytesIO(body))

        urllib.request.urlopen = fake

    def test_the_stack_trace_is_stripped_down_to_the_message(self):
        # The description is forty lines of .NET frames. Relaying it buries the
        # one sentence that says what went wrong.
        server = load()
        self.respond(400, json.dumps({
            "message": "Search failed due to all selected indexers being unavailable",
            "description": "NzbDrone.Core.Exceptions.NzbDroneClientException: Search "
                           "failed\n   at Prowlarr.Api.V1.Search.SearchController"
                           ".GetSearchReleases(SearchResource payload) in /src/...",
        }).encode())

        with self.assertRaises(ToolError) as caught:
            server.api("search", {"query": "x"})
        message = str(caught.exception)
        self.assertIn("all selected indexers being unavailable", message)
        self.assertNotIn("NzbDrone", message)
        self.assertNotIn("SearchController", message)

    def test_a_rejected_key_is_named_as_a_config_problem(self):
        server = load()
        self.respond(401, b"")
        with self.assertRaises(ToolError) as caught:
            server.api("system/status")
        self.assertIn("PROWLARR_API_KEY", str(caught.exception))

    def test_a_body_that_is_not_json_is_still_passed_on(self):
        server = load()
        self.respond(500, b"<html>nginx</html>")
        with self.assertRaises(ToolError) as caught:
            server.api("system/status")
        self.assertIn("nginx", str(caught.exception))


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
