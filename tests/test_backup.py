"""The backup script.

Worth testing precisely because nobody looks at it. A backup job that has been
quietly failing is indistinguishable from one that has been working, right up
until the morning you need it - so the parts that must not go wrong are that a
bad backup is never kept, and that a good one is never pruned to make room for
it.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from support import ROOT, load

backup = load("backup_under_test", "scripts/backup_state.py")


def make_db(path, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT)")
    conn.executemany("INSERT INTO tasks (title) VALUES (?)",
                     [(f"task {n}",) for n in range(rows)])
    conn.commit()
    conn.close()
    return path


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = make_db(os.path.join(self.tmp, "household.db"))
        self.dest = os.path.join(self.tmp, "backups")
        # Capture the script's own output rather than letting it scroll past
        # the test results - and so tests can assert on what it said.
        self.messages = []
        original = backup.log
        backup.log = self.messages.append
        self.addCleanup(lambda: setattr(backup, "log", original))

    def said(self):
        return " ".join(self.messages)

    def run_backup(self, *args):
        return backup.main(["--db", self.db, "--to", self.dest, *args])

    def backups(self):
        return sorted(f for f in os.listdir(self.dest)) if os.path.isdir(self.dest) else []


class TestBackup(BackupCase):
    def test_a_backup_is_a_readable_copy_of_the_data(self):
        self.assertEqual(self.run_backup(), 0)
        [name] = self.backups()
        copy = os.path.join(self.dest, name)
        with sqlite3.connect(copy) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_it_snapshots_a_database_that_is_open_for_writing(self):
        # The reason this uses the backup API and not a file copy. An open
        # connection with uncommitted work must not produce a torn copy.
        live = sqlite3.connect(self.db)
        live.execute("INSERT INTO tasks (title) VALUES ('uncommitted')")
        try:
            self.assertEqual(self.run_backup(), 0)
        finally:
            live.rollback()
            live.close()
        copy = os.path.join(self.dest, self.backups()[0])
        self.assertTrue(backup.integrity_ok(copy))

    def test_retention_keeps_the_newest(self):
        for _ in range(5):
            self.run_backup("--keep", "3")
        kept = self.backups()
        self.assertEqual(len(kept), 3)
        # Names are fixed-width and time-ordered, so lexical order is age order.
        self.assertEqual(kept, sorted(kept))

    def test_keep_zero_keeps_everything(self):
        for _ in range(3):
            self.run_backup("--keep", "0")
        self.assertEqual(len(self.backups()), 3)

    def test_two_runs_in_the_same_second_do_not_overwrite_each_other(self):
        self.run_backup()
        self.run_backup()
        self.assertEqual(len(self.backups()), 2)

    def test_a_missing_database_is_reported_not_papered_over(self):
        self.assertEqual(backup.main(["--db", os.path.join(self.tmp, "nope.db"),
                                      "--to", self.dest]), 1)
        self.assertEqual(self.backups(), [])

    def test_a_corrupt_source_never_replaces_good_backups(self):
        # The way a backup system makes things worse: faithfully overwriting
        # everything good with the damage.
        self.run_backup("--keep", "2")
        before = self.backups()
        with open(self.db, "r+b") as fh:
            fh.seek(0)
            fh.write(b"this is not a database at all, not even slightly")
        self.assertEqual(self.run_backup("--keep", "2"), 2)
        self.assertEqual(self.backups(), before)
        self.assertIn("FAILS its own integrity check", self.said())

    def test_only_real_tables_are_counted(self):
        counts = backup.table_counts(self.db)
        self.assertEqual(counts, {"tasks": 3})


class TestCheckMode(BackupCase):
    def test_check_writes_nothing(self):
        backup.main(["--db", self.db, "--to", self.dest, "--check"])
        self.assertEqual(self.backups(), [])

    def test_check_fails_when_nothing_is_protecting_the_database(self):
        # The point of --check: a scheduled job that stopped running looks
        # exactly like one that is working, unless something asks.
        self.assertEqual(backup.main(["--db", self.db, "--to", self.dest, "--check"]), 3)

    def test_check_passes_once_a_backup_exists(self):
        self.run_backup()
        self.assertEqual(backup.main(["--db", self.db, "--to", self.dest, "--check"]), 0)

    def test_check_flags_a_stale_backup(self):
        self.run_backup()
        old = os.path.join(self.dest, self.backups()[0])
        os.utime(old, (0, 0))  # 1970
        self.assertEqual(backup.main(["--db", self.db, "--to", self.dest, "--check"]), 3)
        self.assertIn("Is the scheduled task running?", self.said())


if __name__ == "__main__":
    unittest.main()
