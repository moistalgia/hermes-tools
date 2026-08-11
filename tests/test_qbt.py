"""Filing decisions, read-back after adding, and the stall that looks like progress.

`qbt-mcp` has one write path, and everything it can get wrong is quiet:

  * a season pack filed into the film library, which nothing complains about
    until Plex has indexed it as a very long movie,
  * an add reported as a download, because `torrents/add` answers `Ok.` to a
    malformed magnet exactly as readily as to a healthy one,
  * a release stalled at 0% with no seeders reported alongside real progress,
    which is how someone waits all evening for a file that was never coming.

None of those raise. Each is a confident wrong answer, which is the kind this
repo tests.

The fake qBittorrent is deliberately obedient: it accepts what it is given and
reports back whatever it was told to report. The point is not to model
qBittorrent, it is to be able to say "this torrent never appears" or "this one
has no seeders" on demand, which you cannot stage against a real client.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

from mcpkit import ToolError  # noqa: E402

HASH = "c9e15763f722f23e98a29decdfae341b98d53056"
OTHER = "a1b2c3d4e5f6071829304a5b6c7d8e9f01234567"


def load(**env):
    settings = {"QBT_URL": "http://qbt.test:8080",
                "QBT_USER": "admin",
                "QBT_PASS": "secret",
                "QBT_MOVIES_PATH": "P:/Movies",
                "QBT_SHOWS_PATH": "P:/Shows",
                "QBT_CONFIRM_TIMEOUT": "1"}
    settings.update(env)
    return support.load("qbt_server", "qbt-mcp/qbt_mcp_server.py", settings)


def magnet(name, infohash=HASH):
    return f"magnet:?xt=urn:btih:{infohash}&dn={name}"


def torrent(name, infohash=HASH, **overrides):
    row = {"hash": infohash, "name": name, "state": "downloading",
           "progress": 0.15, "dlspeed": 4 * 1024 * 1024,
           "size": 3 * 1024 ** 3, "num_seeds": 40,
           "save_path": "P:/Movies", "eta": 1800}
    row.update(overrides)
    return row


class FakeQbt:
    """Just enough qBittorrent. Records adds, answers with fixtures."""

    def __init__(self, rows=None, swallow_adds=False):
        self.rows = list(rows or [])
        # When True, `torrents/add` says Ok. and nothing appears - qBittorrent's
        # behaviour for a duplicate, and indistinguishable from success.
        self.swallow_adds = swallow_adds
        self.adds = []
        self.deletes = []

    def install(self, module):
        module.api = self.api
        return self

    def api(self, path, params=None, method="GET"):
        params = params or {}
        if path == "app/version":
            return "v4.6.5"
        if path == "torrents/info":
            if "hashes" in params:
                wanted = params["hashes"].lower()
                return [r for r in self.rows if r["hash"] == wanted]
            return list(self.rows)
        if path == "torrents/add":
            self.adds.append(params)
            url = params["urls"]
            if not self.swallow_adds and "urn:btih:" in url:
                self.rows.append(torrent(
                    url.split("dn=")[-1],
                    infohash=url.split("urn:btih:")[1][:40],
                    save_path=params.get("savepath")))
            return "Ok."
        if path == "torrents/delete":
            self.deletes.append(params)
            gone = params["hashes"].lower()
            self.rows = [r for r in self.rows if r["hash"] != gone]
            return ""
        raise AssertionError(f"FakeQbt got an unexpected path: {path!r}")


class TestFiling(unittest.TestCase):
    """Where a release lands. Wrong is silent, so this is the quiet one."""

    def setUp(self):
        self.server = load()

    def test_episode_markers_mean_television(self):
        for name in ("Some.Show.S02E05.1080p.WEB-DL",
                     "Some.Show.1x02.720p",
                     "Some.Show.Season.3.1080p",
                     "Some.Show.S01.Complete.1080p",
                     "Some.Show.S04.2160p"):
            with self.subTest(name=name):
                self.assertEqual(self.server.detect_kind(name), "show")

    def test_anything_else_is_a_film(self):
        for name in ("Some.Movie.2019.1080p.BluRay.x265",
                     "Another.Film.2026.2160p.WEB-DL.HDR"):
            with self.subTest(name=name):
                self.assertEqual(self.server.detect_kind(name), "movie")

    def test_a_show_goes_to_the_shows_path_and_a_film_to_the_movies_path(self):
        fake = FakeQbt().install(self.server)
        self.server.download(magnet("Some.Show.S02E05.1080p"))
        self.server.download(magnet("Some.Movie.2019.1080p", infohash=OTHER))
        self.assertEqual(fake.adds[0]["savepath"], "P:/Shows")
        self.assertEqual(fake.adds[1]["savepath"], "P:/Movies")

    def test_an_explicit_kind_overrides_the_detector(self):
        # The escape hatch for the cases a regex over a filename gets wrong.
        fake = FakeQbt().install(self.server)
        result = self.server.download(magnet("Planet.Earth.II.2016.1080p"), kind="show")
        self.assertEqual(result["kind"], "show")
        self.assertEqual(fake.adds[0]["savepath"], "P:/Shows")

    def test_nothing_downloads_when_a_library_path_is_unset(self):
        # Better to refuse than to file everything into one directory and let
        # Plex sort out why the film library has a season pack in it.
        server = load(QBT_SHOWS_PATH="")
        FakeQbt().install(server)
        with self.assertRaises(ToolError) as caught:
            server.download(magnet("Some.Show.S01E01.1080p"))
        self.assertIn("QBT_SHOWS_PATH", str(caught.exception))

    def test_status_reports_the_missing_path_rather_than_looking_healthy(self):
        server = load(QBT_MOVIES_PATH="")
        FakeQbt().install(server)
        result = server.qbt_status()
        self.assertFalse(result["ok"])
        self.assertIn("QBT_MOVIES_PATH", result["summary"])


class TestMagnetReading(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_the_infohash_comes_out_as_lowercase_hex(self):
        self.assertEqual(self.server.magnet_hash(magnet("x", HASH.upper())), HASH)

    def test_a_base32_infohash_resolves_to_the_same_hex(self):
        # Some indexers still emit the 32-char form. Both must land on one hash,
        # or the read-back looks up a torrent that is not there.
        b32 = "ZHQVOY7XELZD5GFCTXWN7LRUDOMNKMCW"
        self.assertEqual(self.server.magnet_hash(f"magnet:?xt=urn:btih:{b32}&dn=x"), HASH)

    def test_a_magnet_with_no_readable_hash_gives_none_rather_than_a_guess(self):
        self.assertIsNone(self.server.magnet_hash("magnet:?dn=no.hash.here"))
        self.assertIsNone(self.server.magnet_hash("http://example.com/x.torrent"))

    def test_a_non_magnet_is_refused_before_qbittorrent_is_touched(self):
        fake = FakeQbt().install(self.server)
        with self.assertRaises(ToolError):
            self.server.download("http://example.com/x.torrent")
        self.assertEqual(fake.adds, [])


class TestReadBack(unittest.TestCase):
    """`Ok.` means accepted. It does not mean added, and it never means running."""

    def setUp(self):
        self.server = load()

    def test_a_confirmed_add_reports_the_state_qbittorrent_actually_holds(self):
        FakeQbt().install(self.server)
        result = self.server.download(magnet("Some.Movie.2019.1080p"))
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["torrent"]["state"], "downloading")
        self.assertEqual(result["infohash"], HASH)

    def test_an_add_that_never_appears_is_reported_unconfirmed_not_successful(self):
        # qBittorrent answers Ok. to a duplicate and adds nothing. Reporting
        # that as a download is how someone waits for a file twice.
        FakeQbt(swallow_adds=True).install(self.server)
        result = self.server.download(magnet("Some.Movie.2019.1080p"))
        self.assertFalse(result["confirmed"])
        self.assertIn("not appeared", result["summary"])

    def test_a_refused_magnet_raises_rather_than_reporting_a_download(self):
        server = load()

        def refuse(path, params=None, method="GET"):
            if path == "torrents/add":
                return "Fails."
            return FakeQbt().api(path, params, method)

        server.api = refuse
        with self.assertRaises(ToolError) as caught:
            server.download(magnet("Some.Movie.2019.1080p"))
        self.assertIn("refused", str(caught.exception))

    def test_a_magnet_without_a_hash_is_added_but_says_it_cannot_confirm(self):
        FakeQbt().install(self.server)
        result = self.server.download("magnet:?dn=No.Hash.Here.2020.1080p")
        self.assertTrue(result["ok"])
        self.assertFalse(result["confirmed"])
        self.assertIsNone(result["infohash"])


class TestStalls(unittest.TestCase):
    """A dead release and a slow one look the same in a progress bar."""

    def setUp(self):
        self.server = load()

    def test_a_stalled_release_at_zero_is_named_not_averaged_in(self):
        FakeQbt(rows=[
            torrent("Healthy.Film.2020.1080p", infohash=HASH),
            torrent("Dead.Film.2021.1080p", infohash=OTHER,
                    state="stalledDL", progress=0.0, dlspeed=0, num_seeds=0),
        ]).install(self.server)
        result = self.server.downloads()
        self.assertIn("Dead.Film.2021.1080p", result["summary"])
        self.assertIn("no seeders", result["summary"])

    def test_a_stalled_release_with_progress_is_not_called_dead(self):
        # Stalled at 60% is a seeder that went away, not a release nobody has.
        FakeQbt(rows=[
            torrent("Paused.Midway.2020.1080p", state="stalledDL", progress=0.6),
        ]).install(self.server)
        self.assertNotIn("no seeders", self.server.downloads()["summary"])

    def test_a_fresh_add_that_is_stalled_says_so_without_calling_it_dead_yet(self):
        FakeQbt(swallow_adds=True, rows=[
            torrent("New.Film.2026.1080p", state="stalledDL", progress=0.0,
                    dlspeed=0, num_seeds=0),
        ]).install(self.server)
        result = self.server.download(magnet("New.Film.2026.1080p"))
        self.assertIn("no seeders", result["summary"])
        self.assertIn("normal for the first few seconds", result["summary"])

    def test_finished_torrents_are_left_out_unless_asked_for(self):
        FakeQbt(rows=[
            torrent("Running.2020.1080p", infohash=HASH),
            torrent("Finished.2019.1080p", infohash=OTHER, state="uploading",
                    progress=1.0),
        ]).install(self.server)
        self.assertEqual(len(self.server.downloads()["torrents"]), 1)
        self.assertEqual(len(self.server.downloads(include_finished=True)["torrents"]), 2)

    def test_an_absurd_eta_is_dropped_rather_than_reported_as_a_number(self):
        # qBittorrent uses 8640000 for "no idea". "in 100 days" is worse than
        # saying nothing.
        FakeQbt(rows=[torrent("Stalled.2020.1080p", eta=8640000)]).install(self.server)
        self.assertIsNone(self.server.downloads()["torrents"][0]["eta_minutes"])


class TestRemoving(unittest.TestCase):
    def setUp(self):
        self.server = load()

    def test_removing_leaves_the_files_unless_told_otherwise(self):
        fake = FakeQbt(rows=[torrent("Wrong.Film.2020.1080p")]).install(self.server)
        result = self.server.download_cancel(HASH)
        self.assertEqual(fake.deletes[0]["deleteFiles"], "false")
        self.assertFalse(result["files_deleted"])

    def test_deleting_files_happens_only_when_asked(self):
        fake = FakeQbt(rows=[torrent("Wrong.Film.2020.1080p")]).install(self.server)
        self.server.download_cancel(HASH, delete_files=True)
        self.assertEqual(fake.deletes[0]["deleteFiles"], "true")

    def test_removing_something_that_is_not_there_says_so_rather_than_succeeding(self):
        FakeQbt().install(self.server)
        with self.assertRaises(ToolError) as caught:
            self.server.download_cancel(OTHER)
        self.assertIn("nothing to remove", str(caught.exception))

    def test_a_delete_that_did_not_take_is_reported_not_assumed(self):
        # `torrents/delete` returns an empty body whether or not it did anything.
        server = load()
        rows = [torrent("Stubborn.Film.2020.1080p")]

        def deaf(path, params=None, method="GET"):
            if path == "torrents/delete":
                return ""            # accepts, changes nothing
            if path == "torrents/info":
                return list(rows)
            raise AssertionError(path)

        server.api = deaf
        with self.assertRaises(ToolError) as caught:
            server.download_cancel(HASH)
        self.assertIn("still in the list", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
