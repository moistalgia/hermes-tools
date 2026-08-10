#!/usr/bin/env python3
"""
Back up household.db, answering the open question DESIGN.md has been asking.

A state store you lose is worse than one you never had, because by the time it
matters you rely on it. This is the "somebody has to set that up" part.

    python scripts/backup_state.py                     # back up now
    python scripts/backup_state.py --keep 30           # keep a month of them
    python scripts/backup_state.py --to E:/backups     # somewhere else
    python scripts/backup_state.py --check             # verify, change nothing

**Not a file copy.** SQLite's own backup API is used deliberately: copying the
file while a write is in flight can capture a torn page, and the copy looks
fine until the day you need it. The backup API takes a consistent snapshot of a
live database with no coordination and no downtime. This is the entire reason
this script exists rather than a one-line `cp` in a scheduled task.

Backups are verified after they are written - `PRAGMA integrity_check` plus a
row count - because an unverified backup is a belief, not a backup.

No dependencies. sqlite3 and the standard library.

Scheduling it, on Windows:

    schtasks /create /tn "hermes-backup" /tr ^
      "python E:\\hermes-mcp\\hermes-tools\\scripts\\backup_state.py --keep 30" ^
      /sc daily /st 03:30

Anywhere with cron:

    30 3 * * *  python /path/to/hermes-tools/scripts/backup_state.py --keep 30
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

DEFAULT_DB = os.environ.get("STATE_DB") or os.path.join(
    os.path.expanduser("~"), ".hermes", "household.db")
DEFAULT_DEST = os.environ.get("STATE_BACKUP_DIR") or os.path.join(
    os.path.expanduser("~"), ".hermes", "backups")
PREFIX = "household-"
SUFFIX = ".db"


def log(message):
    print(message, file=sys.stderr, flush=True)


def read_only(path):
    """Open for reading, and close it properly afterwards.

    `with sqlite3.connect(...)` commits or rolls back on exit - it does NOT
    close the connection. On Windows the leaked handle then blocks os.remove,
    so retention fails with a PermissionError on the one platform this actually
    runs on. Every reader here goes through this.
    """
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_counts(path):
    """Row counts per real table - the cheap sanity check on a backup.

    sqlite_sequence and friends are excluded: they are bookkeeping, they change
    on their own, and counting them turns a useful comparison into noise.
    """
    conn = read_only(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def free_path(directory):
    """A name nothing else is using, that sorts by age.

    Milliseconds rather than seconds, because two runs in the same second would
    otherwise overwrite each other and quietly defeat retention - one file
    standing in for two backups. Every name is the same length, so a plain
    lexical sort is a chronological sort, which is what prune() relies on.
    """
    while True:
        now = datetime.now()
        candidate = os.path.join(
            directory,
            f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}{SUFFIX}")
        if not os.path.exists(candidate):
            return candidate
        time.sleep(0.002)


def integrity_ok(path):
    """True if SQLite is happy with the file. False for anything else.

    A file so damaged that SQLite will not open it at all raises rather than
    reporting corruption, and that is still the answer the caller wants: this
    is not something to back up or restore from.
    """
    try:
        conn = read_only(path)
    except sqlite3.Error:
        return False
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def snapshot(source, destination):
    """A consistent copy of a live database, via SQLite's backup API."""
    src = read_only(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def prune(directory, keep):
    """Drop the oldest backups beyond `keep`. Never touches anything else."""
    existing = sorted(
        f for f in os.listdir(directory)
        if f.startswith(PREFIX) and f.endswith(SUFFIX))
    doomed = existing[:-keep] if keep > 0 and len(existing) > keep else []
    for name in doomed:
        os.remove(os.path.join(directory, name))
    return doomed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Back up the household state database.")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"database to back up (default {DEFAULT_DB})")
    parser.add_argument("--to", default=DEFAULT_DEST, dest="destination",
                        help=f"directory to write into (default {DEFAULT_DEST})")
    parser.add_argument("--keep", type=int, default=14,
                        help="how many backups to retain, oldest pruned. 0 keeps all")
    parser.add_argument("--check", action="store_true",
                        help="verify the source and the newest backup, write nothing")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.db):
        log(f"No database at {args.db}. Nothing to back up.\n"
            "If Hermes has never run, that is expected. If it has, STATE_DB "
            "points somewhere else - check the MCP config for the state server.")
        return 1

    if not integrity_ok(args.db):
        log(f"! {args.db} FAILS its own integrity check. Do not overwrite your "
            "backups with this. Restore from the newest good one instead.")
        return 2

    if args.check:
        counts = table_counts(args.db)
        log(f"{args.db} is intact. " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        if os.path.isdir(args.destination):
            backups = sorted(f for f in os.listdir(args.destination)
                             if f.startswith(PREFIX) and f.endswith(SUFFIX))
            if backups:
                newest = os.path.join(args.destination, backups[-1])
                age_days = (time.time() - os.path.getmtime(newest)) / 86400
                verdict = "ok" if integrity_ok(newest) else "CORRUPT"
                log(f"Newest backup {backups[-1]} is {age_days:.1f} days old ({verdict}). "
                    f"{len(backups)} kept.")
                if age_days > 2:
                    log("! That is older than it should be. Is the scheduled task running?")
                    return 3
            else:
                log(f"! No backups in {args.destination}. Nothing is protecting this.")
                return 3
        else:
            log(f"! {args.destination} does not exist. Nothing is protecting this.")
            return 3
        return 0

    os.makedirs(args.destination, exist_ok=True)
    target = free_path(args.destination)
    snapshot(args.db, target)

    # Verify before pruning. Deleting an old good backup to make room for a bad
    # new one is the way a backup system makes things worse.
    if not integrity_ok(target):
        os.remove(target)
        log(f"! The backup just written to {target} did not verify. It has been "
            "deleted and nothing was pruned. Investigate before trusting this.")
        return 2

    source_counts, backup_counts = table_counts(args.db), table_counts(target)
    if backup_counts != source_counts:
        # Writes during the snapshot are normal and benign; a large drift is not.
        log(f"  note: row counts differ from the source ({source_counts} vs "
            f"{backup_counts}). A write during the snapshot explains a small gap.")

    pruned = prune(args.destination, args.keep)
    total = sum(backup_counts.values())
    log(f"Backed up {args.db} → {target} ({total} rows, verified)."
        + (f" Pruned {len(pruned)}." if pruned else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
