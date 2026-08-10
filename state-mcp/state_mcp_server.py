#!/usr/bin/env python3
"""
state-mcp - the household's shared memory.

Everything the agent needs to remember between conversations, and everything
more than one person in the house needs to see: tasks, the shopping list, what
is in the pantry, the meal plan, household appointments, durable facts about
the house, an unfiled capture inbox, and a log of what the agent itself did.

The point of this server is that it is *shared*. Anyone can add to the shopping
list; anyone can complete a task; everyone sees the result. Every write records
who did it, so "who bought the milk" and "who said they'd call the plumber" are
answerable. That attribution is the whole reason this is not a notes file.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python state_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python state_mcp_server.py household_digest
         python state_mcp_server.py task_add title="clean gutters" area=outside
         python state_mcp_server.py shopping_add item="olive oil" actor=Sam

Environment:
    STATE_DB      path to the SQLite file. Defaults to ~/.hermes/household.db,
                  beside Hermes' own config and deliberately outside the git
                  checkout - a `git pull` must never touch the household's
                  memory. Back this file up; see scripts/backup_state.py.
    STATE_PERSON  who this agent instance speaks for, used when a call does not
                  name an actor. Set it per agent, not per household.

"Today" is the local calendar date of the account running Hermes. If dates look
a day off, that account's timezone is wrong; fix it there rather than adding
offsets here.

No dependencies. sqlite3 and the standard library only.
"""

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

# ~/.hermes, beside Hermes' own config.yaml. Deliberately not the git checkout:
# this file is the household's memory and a `git pull` must never touch it.
DB_PATH = os.environ.get("STATE_DB") or os.path.join(
    os.path.expanduser("~"), ".hermes", "household.db")
DEFAULT_PERSON = os.environ.get("STATE_PERSON", "").strip()

# Task areas are a fixed vocabulary on purpose. Free-text areas drift into
# "kitchen", "Kitchen", and "the kitchen" within a week, and then no filter
# works. Add to this list deliberately.
AREAS = ["house", "kitchen", "outside", "car", "admin", "errand", "pets", "other"]
MEAL_SLOTS = ["breakfast", "lunch", "dinner", "other"]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS people (
    name       TEXT PRIMARY KEY,
    aliases    TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    area         TEXT DEFAULT 'house',
    assignee     TEXT,
    due          TEXT,
    recurrence   TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    notes        TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    completed_by TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS shopping (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item      TEXT NOT NULL,
    qty       TEXT,
    store     TEXT,
    status    TEXT NOT NULL DEFAULT 'needed',
    added_by  TEXT,
    added_at  TEXT NOT NULL,
    bought_by TEXT,
    bought_at TEXT
);
CREATE TABLE IF NOT EXISTS pantry (
    item           TEXT PRIMARY KEY,
    location       TEXT,
    qty            REAL DEFAULT 0,
    unit           TEXT,
    staple         INTEGER DEFAULT 0,
    threshold      REAL DEFAULT 1,
    last_restocked TEXT,
    updated_by     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS meals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    slot       TEXT NOT NULL DEFAULT 'dinner',
    dish       TEXT NOT NULL,
    cook       TEXT,
    recipe_ref TEXT,
    notes      TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(date, slot)
);
CREATE TABLE IF NOT EXISTS appointments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    who        TEXT,
    what       TEXT NOT NULL,
    date       TEXT NOT NULL,
    time       TEXT,
    place      TEXT,
    notes      TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT NOT NULL,
    fact        TEXT NOT NULL,
    recorded_by TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    raw        TEXT NOT NULL,
    source     TEXT,
    from_person TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    filed_to   TEXT,
    filed_at   TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT,
    outcome TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, due);
CREATE INDEX IF NOT EXISTS idx_shopping_status ON shopping(status);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);
CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(date);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
"""

_conn = None


def db():
    global _conn
    if _conn is None:
        parent = os.path.dirname(DB_PATH)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise ToolError(
                    f"Cannot create the directory for STATE_DB ({DB_PATH}): {exc}. "
                    "Pick a path the account running Hermes can write to. Back it "
                    "up once it exists - a state store you lose is worse than "
                    "never having had one, because by then you rely on it."
                )
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(DDL)
        _conn.commit()
    return _conn


def q(sql, args=()):
    return db().execute(sql, args).fetchall()


_in_transaction = False


def write(sql, args=()):
    cur = db().execute(sql, args)
    if not _in_transaction:
        db().commit()
    return cur


@contextmanager
def transaction():
    """Group several writes so a failure part-way through leaves none of them.

    Committing per statement is right for the single-statement tools and wrong
    for the ones that write twice. task_complete marks a task done and then
    creates its recurrence follow-on: a failure between the two left a task
    completed with its recurrence silently gone, which is the worst possible
    outcome because nothing looks broken until the chore never comes back.
    """
    global _in_transaction
    if _in_transaction:  # nested; the outermost block owns the commit
        yield
        return
    conn = db()
    _in_transaction = True
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        _in_transaction = False


# ---------------------------------------------------------------------------
# Time
#
# Everything user-facing is a local calendar date, because "due Friday" is a
# calendar fact and not an instant. Timestamps (journal, created_at) are ISO
# local seconds. Mixing the two is why "today" tasks vanish at 7pm.
# ---------------------------------------------------------------------------

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
RELATIVE = re.compile(r"^(?:in\s+)?\+?(\d+)\s*(day|days|d|week|weeks|w|month|months|m)$", re.I)
RECUR = re.compile(r"^(?:every\s+)?(\d+)?\s*(day|days|week|weeks|month|months|year|years)$", re.I)
NAMED_RECUR = {
    "daily": (1, "day"), "weekly": (1, "week"), "biweekly": (2, "week"),
    "fortnightly": (2, "week"), "monthly": (1, "month"), "quarterly": (3, "month"),
    "yearly": (1, "year"), "annually": (1, "year"),
}


def today():
    return date.today()


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def days_in_month(year, month):
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return [31, 0, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]


def add_interval(anchor, n, unit):
    """Anchor plus n units, clamped to the last valid day of the target month.

    Both the month and year paths have to clamp. 31 Jan + 1 month is 28 Feb, and
    - the case that actually bit - 29 Feb + 1 year is 28 Feb, not a ValueError.
    A yearly task completed on a leap day is a real thing that happens once
    every four years, and it used to raise out of task_complete.
    """
    unit = unit.rstrip("s")
    if unit == "day":
        return anchor + timedelta(days=n)
    if unit == "week":
        return anchor + timedelta(weeks=n)
    if unit == "year":
        year = anchor.year + n
        return date(year, anchor.month, min(anchor.day, days_in_month(year, anchor.month)))
    month = anchor.month - 1 + n
    year = anchor.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(anchor.day, days_in_month(year, month)))


def parse_date(text, field="date"):
    """ISO, 'today', 'tomorrow', a weekday name, or '+3 days' / 'in 2 weeks'."""
    if text is None or str(text).strip() == "":
        return None
    text = str(text).strip().lower()
    if text == "today":
        return today()
    if text == "tomorrow":
        return today() + timedelta(days=1)
    if text == "yesterday":
        return today() - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    m = RELATIVE.match(text)
    if m:
        unit = {"d": "day", "w": "week", "m": "month"}.get(m.group(2), m.group(2))
        return add_interval(today(), int(m.group(1)), unit)
    if text in WEEKDAYS:
        ahead = (WEEKDAYS.index(text) - today().weekday()) % 7 or 7
        return today() + timedelta(days=ahead)
    raise ToolError(
        f"Could not read {field}={text!r}. Use YYYY-MM-DD, 'today', 'tomorrow', "
        "a weekday name, or a relative span like '+3 days' / 'in 2 weeks'."
    )


def parse_recurrence(text):
    """Return (n, unit) or None. Raises on something that looks like a try."""
    if not text or not str(text).strip():
        return None
    text = str(text).strip().lower()
    if text in NAMED_RECUR:
        return NAMED_RECUR[text]
    m = RECUR.match(text)
    if m:
        return int(m.group(1) or 1), m.group(2).rstrip("s")
    raise ToolError(
        f"Could not read recurrence={text!r}. Use 'daily', 'weekly', 'monthly', "
        "'quarterly', 'yearly', or a span like 'every 3 months' / '6 weeks'."
    )


def human_date(iso):
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    delta = (d - today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta < 0:
        return f"{-delta}d overdue"
    if delta <= 6:
        return d.strftime("%a")
    return d.strftime("%a %b %-d") if os.name != "nt" else d.strftime("%a %b %d")


# ---------------------------------------------------------------------------
# People
#
# An unknown name is registered rather than rejected. Rejecting means a new
# housemate blocks every write until someone runs an admin command; silently
# accepting free text means "Sam", "sam", and "Smaa" become three people. So:
# resolve against the roster, register what does not match, and say so.
# ---------------------------------------------------------------------------


def resolve_person(name, note=None):
    if name is None or not str(name).strip():
        if not DEFAULT_PERSON:
            return "unknown"
        name = DEFAULT_PERSON
    name = str(name).strip()
    lowered = name.lower()
    for row in q("SELECT name, aliases FROM people"):
        if row["name"].lower() == lowered:
            return row["name"]
        if lowered in [a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()]:
            return row["name"]
    write("INSERT OR IGNORE INTO people (name, created_at) VALUES (?, ?)", (name, now_iso()))
    if note is not None:
        note.append(f"{name!r} was not on the roster and has been added as a new person.")
    return name


def actor_note(actor):
    """Resolve an actor and collect the 'new person' note in one step."""
    notes = []
    resolved = resolve_person(actor, notes)
    return resolved, notes


def valid_area(area):
    if not area:
        return "house"
    area = str(area).strip().lower()
    if area not in AREAS:
        raise ToolError(f"Unknown area {area!r}.", known_areas=AREAS)
    return area


# Annotations rather than data: worth saying when there is something to say,
# noise in every payload otherwise. Everything else keeps its shape - a list
# tool that returns no `items` key on an empty list is a tool whose caller has
# to special-case the commonest outcome, and that special case gets forgotten.
ANNOTATION_KEYS = {"notes", "warnings"}


def ok(summary, **extra):
    payload = {"ok": True, "summary": summary}
    payload.update({
        k: v for k, v in extra.items()
        if v is not None and v != "" and not (k in ANNOTATION_KEYS and not v)
    })
    return payload


# ---------------------------------------------------------------------------
# Status and roster
# ---------------------------------------------------------------------------


@tool("Check that the state store is reachable and report what is in it. Run this first when anything looks wrong.")
def state_status():
    counts = {
        "open tasks": q("SELECT COUNT(*) c FROM tasks WHERE status='open'")[0]["c"],
        "shopping items needed": q("SELECT COUNT(*) c FROM shopping WHERE status='needed'")[0]["c"],
        "pantry items": q("SELECT COUNT(*) c FROM pantry")[0]["c"],
        "meals planned": q("SELECT COUNT(*) c FROM meals WHERE date >= ?", (today().isoformat(),))[0]["c"],
        "facts": q("SELECT COUNT(*) c FROM facts")[0]["c"],
        "pending captures": q("SELECT COUNT(*) c FROM capture WHERE status='pending'")[0]["c"],
    }
    people = [r["name"] for r in q("SELECT name FROM people ORDER BY name")]
    warnings = []
    if not DEFAULT_PERSON:
        warnings.append(
            "STATE_PERSON is not set, so writes that do not name an actor are "
            "attributed to 'unknown'. Set it in the MCP config for this agent."
        )
    if not os.access(DB_PATH, os.W_OK):
        warnings.append(f"{DB_PATH} is not writable. Every write tool will fail.")
    return ok(
        f"State store at {DB_PATH}. " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".",
        database=DB_PATH, counts=counts, people=people,
        default_person=DEFAULT_PERSON or None, warnings=warnings,
    )


@tool(
    "Add a person to the household roster, with optional spoken aliases.",
    {"name": s("Canonical name, as it should be displayed."),
     "aliases": s("Comma-separated other names this person is called.")},
    required=["name"],
)
def person_add(name, aliases=""):
    name = name.strip()
    if not name:
        raise ToolError("A person needs a name.")
    write(
        "INSERT INTO people (name, aliases, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET aliases=excluded.aliases",
        (name, aliases or "", now_iso()),
    )
    return ok(f"{name} is on the household roster." + (f" Also known as: {aliases}." if aliases else ""))


@tool("List everyone on the household roster and the names each answers to.")
def people_list():
    rows = q("SELECT name, aliases FROM people ORDER BY name")
    if not rows:
        return ok("Nobody on the roster yet. Add people with person_add so writes can be attributed.")
    parts = [r["name"] + (f" ({r['aliases']})" if r["aliases"] else "") for r in rows]
    return ok("Household: " + ", ".join(parts) + ".",
              people=[dict(r) for r in rows])


# ---------------------------------------------------------------------------
# The one-call view
# ---------------------------------------------------------------------------


@tool(
    "The whole household in one call: what is due, what is planned, what is "
    "unfiled. Use this to answer 'what's going on' and to build the daily "
    "brief. Anything unusual comes first.",
    {"person": s("Optional. Lead with this person's items, but still show everything."),
     "days": i("How far ahead to look for meals and appointments.", default=3)},
)
def household_digest(person=None, days=3):
    horizon = (today() + timedelta(days=max(1, days))).isoformat()
    lines = []

    overdue = q("SELECT * FROM tasks WHERE status='open' AND due IS NOT NULL AND due < ? ORDER BY due",
                (today().isoformat(),))
    due_now = q("SELECT * FROM tasks WHERE status='open' AND due = ?", (today().isoformat(),))
    unfiled = q("SELECT COUNT(*) c FROM capture WHERE status='pending'")[0]["c"]
    low = [r for r in q("SELECT * FROM pantry WHERE staple=1") if (r["qty"] or 0) <= (r["threshold"] or 0)]
    shopping = q("SELECT * FROM shopping WHERE status='needed' ORDER BY added_at")
    appts = q("SELECT * FROM appointments WHERE date BETWEEN ? AND ? ORDER BY date, time",
              (today().isoformat(), horizon))
    meals = q("SELECT * FROM meals WHERE date BETWEEN ? AND ? ORDER BY date", (today().isoformat(), horizon))

    if overdue:
        lines.append(f"! {len(overdue)} overdue: " + "; ".join(
            f"{r['title']} ({human_date(r['due'])}{', ' + r['assignee'] if r['assignee'] else ''})"
            for r in overdue[:4]))
    if unfiled:
        lines.append(f"! {unfiled} unfiled capture item{'s' if unfiled != 1 else ''} waiting to be sorted")
    if low:
        lines.append("! staples low: " + ", ".join(r["item"] for r in low[:6]))
    if due_now:
        lines.append("due today: " + "; ".join(
            r["title"] + (f" ({r['assignee']})" if r["assignee"] else "") for r in due_now))
    if appts:
        lines.append("appointments: " + "; ".join(
            f"{r['what']} {human_date(r['date'])}{' ' + r['time'] if r['time'] else ''}"
            f"{' — ' + r['who'] if r['who'] else ''}" for r in appts[:5]))
    if meals:
        lines.append("meals: " + "; ".join(
            f"{human_date(r['date'])} {r['dish']}" + (f" ({r['cook']})" if r["cook"] else "")
            for r in meals))
    else:
        lines.append("meals: nothing planned")
    if shopping:
        lines.append(f"shopping: {len(shopping)} item{'s' if len(shopping) != 1 else ''} — " +
                     ", ".join(r["item"] for r in shopping[:8]))

    mine = []
    if person:
        who = resolve_person(person)
        mine = [dict(r) for r in q(
            "SELECT * FROM tasks WHERE status='open' AND assignee=? ORDER BY due IS NULL, due", (who,))]
        if mine:
            lines.insert(0, f"{who}: " + "; ".join(
                f"{t['title']}{' (' + human_date(t['due']) + ')' if t['due'] else ''}" for t in mine[:4]))

    headline = "Household — nothing needs attention." if not (overdue or unfiled or low) else "Household"
    return ok(
        headline + "\n  " + "\n  ".join(lines) if lines else headline,
        needs_attention=bool(overdue or unfiled or low),
        overdue=[dict(r) for r in overdue],
        due_today=[dict(r) for r in due_now],
        for_person=mine,
        low_staples=[dict(r) for r in low],
        shopping=[dict(r) for r in shopping],
        appointments=[dict(r) for r in appts],
        meals=[dict(r) for r in meals],
        unfiled_captures=unfiled,
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@tool(
    "Add a household task. Anyone can add; everyone sees it.",
    {"title": s("What needs doing, as a short imperative."),
     "area": s("Which part of household life this belongs to.", enum=AREAS),
     "assignee": s("Who is responsible. Leave empty for unassigned."),
     "due": s("YYYY-MM-DD, 'today', 'tomorrow', a weekday, or '+3 days'."),
     "recurrence": s("'weekly', 'quarterly', 'every 3 months'. Recurs from when it is completed, not from a fixed calendar."),
     "notes": s("Anything worth remembering when the time comes."),
     "actor": s("Who is adding this. Defaults to STATE_PERSON.")},
    required=["title"],
)
def task_add(title, area="house", assignee=None, due=None, recurrence=None, notes=None, actor=None):
    title = (title or "").strip()
    if not title:
        raise ToolError("A task needs a title.")
    who, notes_out = actor_note(actor)
    area = valid_area(area)
    due_d = parse_date(due, "due")
    parse_recurrence(recurrence)  # validate now, not at completion time
    assigned = resolve_person(assignee, notes_out) if assignee else None
    cur = write(
        "INSERT INTO tasks (title, area, assignee, due, recurrence, notes, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (title, area, assigned, due_d.isoformat() if due_d else None,
         recurrence, notes, who, now_iso()),
    )
    bits = [f"#{cur.lastrowid} {title}", area]
    if assigned:
        bits.append(f"for {assigned}")
    if due_d:
        bits.append(f"due {human_date(due_d.isoformat())}")
    if recurrence:
        bits.append(f"repeats {recurrence}")
    return ok("Added: " + ", ".join(bits) + ".", task_id=cur.lastrowid, notes=notes_out)


@tool(
    "List open tasks, soonest first. Answers 'what should we do', so it leads "
    "with what is actually due rather than handing back everything.",
    {"person": s("Only this person's tasks, plus unassigned ones."),
     "area": s("Only this area.", enum=AREAS),
     "window": s("'overdue', 'today', 'week', or 'all'.", default="all",
                 enum=["overdue", "today", "week", "all"]),
     "include_done": b("Include recently completed tasks.")},
)
def task_list(person=None, area=None, window="all", include_done=False):
    sql = "SELECT * FROM tasks WHERE status = ?"
    args = ["open"]
    if person:
        who = resolve_person(person)
        sql += " AND (assignee = ? OR assignee IS NULL)"
        args.append(who)
    if area:
        sql += " AND area = ?"
        args.append(valid_area(area))
    if window == "overdue":
        sql += " AND due IS NOT NULL AND due < ?"
        args.append(today().isoformat())
    elif window == "today":
        sql += " AND due IS NOT NULL AND due <= ?"
        args.append(today().isoformat())
    elif window == "week":
        sql += " AND due IS NOT NULL AND due <= ?"
        args.append((today() + timedelta(days=7)).isoformat())
    rows = q(sql + " ORDER BY due IS NULL, due, id", args)

    done = []
    if include_done:
        done = q("SELECT * FROM tasks WHERE status='done' AND completed_at >= ? ORDER BY completed_at DESC",
                 ((today() - timedelta(days=7)).isoformat(),))

    if not rows:
        return ok("No open tasks matching that." + (f" {len(done)} completed in the last week." if done else ""),
                  tasks=[], completed=[dict(r) for r in done])
    parts = []
    for r in rows:
        line = f"#{r['id']} {r['title']}"
        extra = [x for x in (human_date(r["due"]) if r["due"] else "", r["assignee"] or "", r["area"]) if x]
        parts.append(line + (f" ({', '.join(extra)})" if extra else ""))
    return ok(f"{len(rows)} open — " + "; ".join(parts),
              tasks=[dict(r) for r in rows], completed=[dict(r) for r in done])


@tool(
    "Mark a task done. If it recurs, the next one is created from today, not "
    "from the old due date — a filter changed late is due three months from "
    "when it was changed.",
    {"task_id": i("The id from task_add or task_list."),
     "actor": s("Who did it. Defaults to STATE_PERSON."),
     "notes": s("Anything worth recording about how it went.")},
    required=["task_id"],
)
def task_complete(task_id, actor=None, notes=None):
    rows = q("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        open_ids = [f"#{r['id']} {r['title']}" for r in
                    q("SELECT id, title FROM tasks WHERE status='open' ORDER BY id LIMIT 8")]
        raise ToolError(f"No task #{task_id}.", open_tasks=open_ids)
    task = rows[0]
    if task["status"] == "done":
        return ok(f"#{task_id} {task['title']} was already completed by "
                  f"{task['completed_by'] or 'someone'} on {task['completed_at']}.")
    who, notes_out = actor_note(actor)
    recur = parse_recurrence(task["recurrence"])

    # Completing and re-creating are one operation. Rolled back together, so a
    # recurring task can never end up done with its next occurrence missing.
    follow_on = None
    with transaction():
        write("UPDATE tasks SET status='done', completed_by=?, completed_at=?, notes=COALESCE(?, notes) WHERE id=?",
              (who, now_iso(), notes, task_id))
        if recur:
            next_due = add_interval(today(), *recur)
            cur = write(
                "INSERT INTO tasks (title, area, assignee, due, recurrence, notes, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task["title"], task["area"], task["assignee"], next_due.isoformat(),
                 task["recurrence"], task["notes"], who, now_iso()),
            )
            follow_on = {"task_id": cur.lastrowid, "due": next_due.isoformat()}
    summary = f"Done: {task['title']} ({who})."
    if follow_on:
        summary += f" Next one is #{follow_on['task_id']}, due {human_date(follow_on['due'])}."
    return ok(summary, next_task=follow_on, notes=notes_out)


@tool(
    "Change an existing task — reschedule it, reassign it, retitle it, or "
    "adjust its recurrence. Only the fields you pass are touched; everything "
    "else is left alone. Use this rather than dropping a task and adding a new "
    "one, which loses who created it and when.",
    {"task_id": i("The id from task_add or task_list."),
     "title": s("New title."),
     "area": s("New area.", enum=AREAS),
     "assignee": s("Who is responsible now. Pass 'none' to unassign."),
     "due": s("New due date, or 'none' to clear it."),
     "recurrence": s("New recurrence, or 'none' to stop it repeating."),
     "notes": s("Replacement notes."),
     "actor": s("Who is making the change.")},
    required=["task_id"],
)
def task_update(task_id, title=None, area=None, assignee=None, due=None,
                recurrence=None, notes=None, actor=None):
    rows = q("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        open_ids = [f"#{r['id']} {r['title']}" for r in
                    q("SELECT id, title FROM tasks WHERE status='open' ORDER BY id LIMIT 8")]
        raise ToolError(f"No task #{task_id}.", open_tasks=open_ids)
    task = rows[0]
    if task["status"] != "open":
        raise ToolError(
            f"#{task_id} is {task['status']}, not open. Editing a finished task "
            "would rewrite history. Add a new task if it needs doing again.")

    who, notes_out = actor_note(actor)
    sets, args, changed = [], [], []

    # 'none' is the explicit clear. Leaving a field out means "don't touch it",
    # so there has to be some way to say "make this empty" - and an empty string
    # is indistinguishable from an omitted argument by the time it arrives here.
    def clearing(value):
        return str(value).strip().lower() in ("none", "null", "clear", "-")

    if title is not None and title.strip():
        sets.append("title=?"); args.append(title.strip())
        changed.append(f"title → {title.strip()}")
    if area is not None:
        sets.append("area=?"); args.append(valid_area(area))
        changed.append(f"area → {valid_area(area)}")
    if assignee is not None:
        if clearing(assignee):
            sets.append("assignee=NULL"); changed.append("unassigned")
        else:
            who_r = resolve_person(assignee, notes_out)
            sets.append("assignee=?"); args.append(who_r)
            changed.append(f"assigned to {who_r}")
    if due is not None:
        if clearing(due):
            sets.append("due=NULL"); changed.append("due date cleared")
        else:
            due_d = parse_date(due, "due")
            sets.append("due=?"); args.append(due_d.isoformat())
            changed.append(f"due {human_date(due_d.isoformat())}")
    if recurrence is not None:
        if clearing(recurrence):
            sets.append("recurrence=NULL"); changed.append("no longer repeats")
        else:
            parse_recurrence(recurrence)  # validate before storing
            sets.append("recurrence=?"); args.append(recurrence)
            changed.append(f"repeats {recurrence}")
    if notes is not None:
        sets.append("notes=?"); args.append(notes if not clearing(notes) else None)
        changed.append("notes updated")

    if not sets:
        raise ToolError(
            f"Nothing to change on #{task_id}. Pass at least one of title, area, "
            "assignee, due, recurrence, or notes.")
    args.append(task_id)
    write(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
    return ok(f"#{task_id} {task['title']}: " + ", ".join(changed) + f" ({who}).",
              task_id=task_id, changed=changed, notes=notes_out)


@tool(
    "Drop a task that is no longer relevant. Different from completing it, and "
    "kept so the record stays honest.",
    {"task_id": i("The id to drop."), "reason": s("Why it no longer applies."),
     "actor": s("Who dropped it.")},
    required=["task_id"],
)
def task_drop(task_id, reason=None, actor=None):
    if not q("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
        raise ToolError(f"No task #{task_id}.")
    who, _ = actor_note(actor)
    write("UPDATE tasks SET status='dropped', completed_by=?, completed_at=?, "
          "notes=COALESCE(?, notes) WHERE id=?", (who, now_iso(), reason, task_id))
    return ok(f"Dropped #{task_id}." + (f" Reason: {reason}" if reason else ""))


# ---------------------------------------------------------------------------
# Shopping and pantry
#
# Two tables on purpose. The shopping list is what to buy; the pantry is what
# is in the house. Collapsing them is why "we have olive oil" and "olive oil is
# on the list" contradict each other.
# ---------------------------------------------------------------------------


@tool(
    "Add something to the shared shopping list.",
    {"item": s("What to buy."), "qty": s("How much, free text: '2', 'a big one', '500g'."),
     "store": s("Where, if it matters."), "actor": s("Who added it.")},
    required=["item"],
)
def shopping_add(item, qty=None, store=None, actor=None):
    item = (item or "").strip()
    if not item:
        raise ToolError("Nothing named to buy.")
    # qty is free text ("2", "a big one", "500g") but the CLI coerces bare
    # digits to int, so normalise before anything tries to concatenate it.
    qty = str(qty) if qty is not None and str(qty).strip() else None
    who, notes_out = actor_note(actor)
    existing = q("SELECT * FROM shopping WHERE status='needed' AND LOWER(item)=?", (item.lower(),))
    if existing:
        return ok(f"{item} is already on the list (added by {existing[0]['added_by'] or 'someone'}).",
                  task_id=existing[0]["id"], duplicate=True)
    cur = write("INSERT INTO shopping (item, qty, store, added_by, added_at) VALUES (?,?,?,?,?)",
                (item, qty, store, who, now_iso()))
    count = q("SELECT COUNT(*) c FROM shopping WHERE status='needed'")[0]["c"]
    return ok(f"Added {item}{f' ({qty})' if qty else ''} to the list. "
              f"{count} item{'s' if count != 1 else ''} now.",
              item_id=cur.lastrowid, notes=notes_out)


@tool(
    "The current shopping list.",
    {"store": s("Only items for this store.")},
)
def shopping_list(store=None):
    sql = "SELECT * FROM shopping WHERE status='needed'"
    args = []
    if store:
        sql += " AND LOWER(COALESCE(store,'')) = ?"
        args.append(store.lower())
    rows = q(sql + " ORDER BY added_at", args)
    if not rows:
        return ok("Shopping list is empty.", items=[])
    parts = [f"{r['item']}" + (f" ({r['qty']})" if r["qty"] else "") for r in rows]
    return ok(f"{len(rows)} to buy: " + ", ".join(parts), items=[dict(r) for r in rows])


@tool(
    "Mark shopping items bought. Pass items as a comma-separated list, or "
    "leave empty with all=true to clear the whole list after a shop.",
    {"items": s("Comma-separated item names."), "all": b("Mark everything bought."),
     "actor": s("Who did the shopping.")},
)
def shopping_bought(items=None, all=False, actor=None):
    who, notes_out = actor_note(actor)
    rows = q("SELECT * FROM shopping WHERE status='needed'")
    if not rows:
        return ok("Nothing on the list to mark bought.")
    if all:
        targets = rows
    else:
        wanted = [x.strip().lower() for x in (items or "").split(",") if x.strip()]
        if not wanted:
            raise ToolError("Name the items bought, or pass all=true.",
                            on_list=[r["item"] for r in rows])
        targets = [r for r in rows if r["item"].lower() in wanted]
        missed = [w for w in wanted if w not in [r["item"].lower() for r in rows]]
        if not targets:
            raise ToolError("None of those are on the list.", on_list=[r["item"] for r in rows])
        if missed:
            notes_out.append("Not on the list, so ignored: " + ", ".join(missed))
    # One shop is one operation: a half-marked list after an interrupted call
    # is how you end up buying the same thing twice.
    with transaction():
        for r in targets:
            write("UPDATE shopping SET status='bought', bought_by=?, bought_at=? WHERE id=?",
                  (who, now_iso(), r["id"]))
            # Buying a staple restocks it. The pantry is the reason to bother.
            if q("SELECT 1 FROM pantry WHERE LOWER(item)=?", (r["item"].lower(),)):
                write("UPDATE pantry SET qty = MAX(qty, threshold + 1), last_restocked=?, "
                      "updated_by=?, updated_at=? WHERE LOWER(item)=?",
                      (today().isoformat(), who, now_iso(), r["item"].lower()))
    left = q("SELECT COUNT(*) c FROM shopping WHERE status='needed'")[0]["c"]
    return ok(f"Marked {len(targets)} bought ({who}). {left} left on the list.", notes=notes_out)


@tool(
    "Take something off the shopping list without pretending it was bought. "
    "This is the tool for a typo, a duplicate, or a change of mind — do NOT "
    "use shopping_bought for those, because buying a staple restocks the "
    "pantry and would leave the house believing it has something it does not.",
    {"items": s("Comma-separated item names, as they appear on the list."),
     "reason": s("Why it is coming off: 'typo', 'already have some', 'changed my mind'."),
     "actor": s("Who is removing it.")},
    required=["items"],
)
def shopping_remove(items, reason=None, actor=None):
    who, notes_out = actor_note(actor)
    rows = q("SELECT * FROM shopping WHERE status='needed'")
    if not rows:
        return ok("Shopping list is already empty.")
    wanted = [x.strip().lower() for x in (items or "").split(",") if x.strip()]
    if not wanted:
        raise ToolError("Name the items to remove.", on_list=[r["item"] for r in rows])
    targets = [r for r in rows if r["item"].lower() in wanted]
    if not targets:
        raise ToolError("None of those are on the list.", on_list=[r["item"] for r in rows])
    missed = [w for w in wanted if w not in [r["item"].lower() for r in rows]]
    if missed:
        notes_out.append("Not on the list, so ignored: " + ", ".join(missed))
    with transaction():
        for r in targets:
            write("UPDATE shopping SET status='removed', bought_by=?, bought_at=? WHERE id=?",
                  (who, now_iso(), r["id"]))
    left = q("SELECT COUNT(*) c FROM shopping WHERE status='needed'")[0]["c"]
    return ok(f"Removed {', '.join(r['item'] for r in targets)} from the list ({who})"
              + (f" — {reason}" if reason else "") + f". {left} left.", notes=notes_out)


@tool(
    "Set what is in the pantry. Staples are the things worth reordering "
    "automatically when they run low.",
    {"item": s("What it is."), "qty": s("How many/much. Numeric."),
     "location": s("Where it lives: pantry, freezer, garage."),
     "unit": s("Optional unit for display: 'bottles', 'kg'."),
     "staple": b("Track this and warn when it drops to the threshold."),
     "threshold": s("Warn at or below this quantity. Numeric. Default 1."),
     "actor": s("Who is recording this.")},
    required=["item"],
)
def pantry_set(item, qty=None, location=None, unit=None, staple=False, threshold=None, actor=None):
    item = (item or "").strip()
    if not item:
        raise ToolError("Nothing named.")
    who, notes_out = actor_note(actor)
    try:
        qty_v = float(qty) if qty is not None and str(qty).strip() != "" else 0.0
        thr_v = float(threshold) if threshold is not None and str(threshold).strip() != "" else 1.0
    except ValueError:
        raise ToolError("qty and threshold must be numbers. Use the unit field for 'bottles' or 'kg'.")
    write(
        "INSERT INTO pantry (item, location, qty, unit, staple, threshold, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET "
        "location=COALESCE(excluded.location, pantry.location), qty=excluded.qty, "
        "unit=COALESCE(excluded.unit, pantry.unit), staple=excluded.staple, "
        "threshold=excluded.threshold, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (item, location, qty_v, unit, 1 if staple else 0, thr_v, who, now_iso()),
    )
    warn = " Below threshold — worth adding to the shopping list." if staple and qty_v <= thr_v else ""
    return ok(f"{item}: {qty_v:g}{' ' + unit if unit else ''}"
              f"{' in ' + location if location else ''}.{warn}", notes=notes_out)


@tool(
    "Delete a pantry row entirely — for something recorded by mistake or no "
    "longer kept in the house. To say something has run out, use pantry_set "
    "with qty=0 instead: that keeps the threshold and the restock history, and "
    "a staple at zero is what puts it on the shopping list.",
    {"item": s("The pantry item to delete."), "actor": s("Who is removing it.")},
    required=["item"],
)
def pantry_remove(item, actor=None):
    item = (item or "").strip()
    rows = q("SELECT * FROM pantry WHERE LOWER(item)=?", (item.lower(),))
    if not rows:
        known = [r["item"] for r in q("SELECT item FROM pantry ORDER BY item LIMIT 15")]
        raise ToolError(f"Nothing in the pantry called {item!r}.", in_pantry=known)
    row = rows[0]
    who, notes_out = actor_note(actor)
    write("DELETE FROM pantry WHERE LOWER(item)=?", (item.lower(),))
    warn = ""
    if row["staple"]:
        warn = (" It was a staple, so nothing will warn when it runs low any more.")
    return ok(f"Removed {row['item']} from the pantry ({who}).{warn}", notes=notes_out)


@tool("Staples at or below their threshold — what to put on the shopping list.")
def pantry_low():
    rows = [r for r in q("SELECT * FROM pantry WHERE staple=1 ORDER BY item")
            if (r["qty"] or 0) <= (r["threshold"] or 0)]
    if not rows:
        return ok("No staples are low.", items=[])
    on_list = {r["item"].lower() for r in q("SELECT item FROM shopping WHERE status='needed'")}
    parts, to_add = [], []
    for r in rows:
        marker = " (already on the list)" if r["item"].lower() in on_list else ""
        if not marker:
            to_add.append(r["item"])
        parts.append(f"{r['item']} {r['qty']:g}{' ' + r['unit'] if r['unit'] else ''}{marker}")
    return ok(f"{len(rows)} low: " + ", ".join(parts),
              items=[dict(r) for r in rows], not_yet_on_list=to_add)


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


@tool(
    "Put a meal on the plan. One dish per date and slot; planning over an "
    "existing entry replaces it.",
    {"date": s("YYYY-MM-DD, 'today', 'tomorrow', or a weekday name."),
     "dish": s("What is being made."),
     "slot": s("Which meal.", default="dinner", enum=MEAL_SLOTS),
     "cook": s("Who is cooking."), "recipe_ref": s("URL, book and page, or a note about where it came from."),
     "notes": s("Anything else — 'needs thawing', 'doubles well'."),
     "actor": s("Who is planning it.")},
    required=["date", "dish"],
)
def meal_plan(date, dish, slot="dinner", cook=None, recipe_ref=None, notes=None, actor=None):
    d = parse_date(date, "date")
    slot = (slot or "dinner").lower()
    if slot not in MEAL_SLOTS:
        raise ToolError(f"Unknown slot {slot!r}.", known_slots=MEAL_SLOTS)
    who, notes_out = actor_note(actor)
    cook_r = resolve_person(cook, notes_out) if cook else None
    prior = q("SELECT dish FROM meals WHERE date=? AND slot=?", (d.isoformat(), slot))
    write(
        "INSERT INTO meals (date, slot, dish, cook, recipe_ref, notes, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(date, slot) DO UPDATE SET "
        "dish=excluded.dish, cook=excluded.cook, recipe_ref=excluded.recipe_ref, "
        "notes=excluded.notes, created_by=excluded.created_by, created_at=excluded.created_at",
        (d.isoformat(), slot, dish, cook_r, recipe_ref, notes, who, now_iso()),
    )
    replaced = f" (replacing {prior[0]['dish']})" if prior else ""
    return ok(f"{slot.title()} {human_date(d.isoformat())}: {dish}"
              f"{' — ' + cook_r if cook_r else ''}{replaced}.", notes=notes_out)


@tool(
    "The meal plan, and which days have nothing on them.",
    {"days": i("How many days ahead.", default=7), "start": s("First day. Defaults to today.")},
)
def meal_week(days=7, start=None):
    first = parse_date(start, "start") or today()
    last = first + timedelta(days=max(1, days) - 1)
    rows = q("SELECT * FROM meals WHERE date BETWEEN ? AND ? ORDER BY date, slot",
             (first.isoformat(), last.isoformat()))
    planned = {r["date"] for r in rows if r["slot"] == "dinner"}
    gaps = [(first + timedelta(days=n)) for n in range((last - first).days + 1)
            if (first + timedelta(days=n)).isoformat() not in planned]
    if not rows:
        return ok(f"Nothing planned between {first} and {last}.", meals=[],
                  unplanned=[g.isoformat() for g in gaps])
    parts = [f"{human_date(r['date'])} {r['slot']}: {r['dish']}" + (f" ({r['cook']})" if r["cook"] else "")
             for r in rows]
    tail = ""
    if gaps:
        tail = " No dinner planned: " + ", ".join(human_date(g.isoformat()) for g in gaps) + "."
    return ok("; ".join(parts) + "." + tail,
              meals=[dict(r) for r in rows], unplanned=[g.isoformat() for g in gaps])


# ---------------------------------------------------------------------------
# Appointments
#
# Household-visible, deliberately separate from anyone's personal calendar.
# "The dog is at the vet Thursday" is something the house needs and nobody
# wants sitting in one person's work calendar.
# ---------------------------------------------------------------------------


@tool(
    "Record a household appointment — the kind everyone needs to know about.",
    {"what": s("What it is."), "date": s("YYYY-MM-DD, 'tomorrow', a weekday, '+2 weeks'."),
     "time": s("Local time as HH:MM, if it has one."), "who": s("Who it concerns."),
     "place": s("Where."), "notes": s("Anything to bring or know."),
     "actor": s("Who is recording it.")},
    required=["what", "date"],
)
def appointment_add(what, date, time=None, who=None, place=None, notes=None, actor=None):
    d = parse_date(date, "date")
    time = str(time).strip() if time is not None and str(time).strip() else None
    if time and not re.match(r"^\d{1,2}:\d{2}$", time):
        raise ToolError(f"Could not read time={time!r}. Use 24-hour HH:MM, or leave it out.")
    actor_r, notes_out = actor_note(actor)
    who_r = resolve_person(who, notes_out) if who else None
    cur = write(
        "INSERT INTO appointments (who, what, date, time, place, notes, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (who_r, what, d.isoformat(), time, place, notes, actor_r, now_iso()),
    )
    return ok(f"#{cur.lastrowid} {what} — {human_date(d.isoformat())}"
              f"{f' at {time}' if time else ''}{f', {place}' if place else ''}"
              f"{f' ({who_r})' if who_r else ''}.", appointment_id=cur.lastrowid, notes=notes_out)


@tool(
    "Upcoming household appointments.",
    {"days": i("How far ahead.", default=14), "who": s("Only this person's.")},
)
def appointment_list(days=14, who=None):
    sql = "SELECT * FROM appointments WHERE date BETWEEN ? AND ?"
    args = [today().isoformat(), (today() + timedelta(days=max(1, days))).isoformat()]
    if who:
        sql += " AND who = ?"
        args.append(resolve_person(who))
    rows = q(sql + " ORDER BY date, time", args)
    if not rows:
        return ok(f"Nothing on the household calendar in the next {days} days.", appointments=[])
    parts = [f"{human_date(r['date'])}{' ' + r['time'] if r['time'] else ''} {r['what']}"
             f"{' (' + r['who'] + ')' if r['who'] else ''}" for r in rows]
    return ok("; ".join(parts), appointments=[dict(r) for r in rows])


@tool(
    "Cancel a household appointment. Deleted rather than marked cancelled: an "
    "appointment nobody is going to is noise on a calendar everyone reads.",
    {"appointment_id": i("The id from appointment_add or appointment_list."),
     "reason": s("Why, if it is worth recording."),
     "actor": s("Who cancelled it.")},
    required=["appointment_id"],
)
def appointment_cancel(appointment_id, reason=None, actor=None):
    rows = q("SELECT * FROM appointments WHERE id=?", (appointment_id,))
    if not rows:
        upcoming = [f"#{r['id']} {r['what']} {human_date(r['date'])}" for r in
                    q("SELECT * FROM appointments WHERE date >= ? ORDER BY date LIMIT 8",
                      (today().isoformat(),))]
        raise ToolError(f"No appointment #{appointment_id}.", upcoming=upcoming)
    appt = rows[0]
    who, notes_out = actor_note(actor)
    with transaction():
        write("DELETE FROM appointments WHERE id=?", (appointment_id,))
        # Cancellations are the thing someone asks about a week later, so this
        # one deletion is worth a journal line the digest will never show.
        write("INSERT INTO journal (ts, actor, action, target, detail, outcome) "
              "VALUES (?,?,?,?,?,?)",
              (now_iso(), who, "appointment_cancel", appt["what"],
               f"{appt['date']}{' ' + appt['time'] if appt['time'] else ''}"
               + (f" — {reason}" if reason else ""), "ok"))
    return ok(f"Cancelled {appt['what']} on {human_date(appt['date'])} ({who})."
              + (f" Reason: {reason}" if reason else ""), notes=notes_out)


# ---------------------------------------------------------------------------
# Facts
#
# Deliberately loose. Everything true about the house that lives nowhere else:
# filter sizes, paint colours, the wifi password hint, which breaker is which.
# This becomes the most-read table faster than you expect.
# ---------------------------------------------------------------------------


@tool(
    "Record a durable fact about the house or household. Re-recording the same "
    "subject keeps the history rather than overwriting it.",
    {"subject": s("What the fact is about: 'furnace filter', 'living room paint'."),
     "fact": s("The fact itself, written so it is useful a year from now."),
     "actor": s("Who recorded it.")},
    required=["subject", "fact"],
)
def fact_record(subject, fact, actor=None):
    who, notes_out = actor_note(actor)
    prior = q("SELECT fact FROM facts WHERE LOWER(subject)=? ORDER BY id DESC LIMIT 1", (subject.lower(),))
    write("INSERT INTO facts (subject, fact, recorded_by, recorded_at) VALUES (?,?,?,?)",
          (subject, fact, who, now_iso()))
    note = f" Previously: {prior[0]['fact']}" if prior else ""
    return ok(f"Recorded — {subject}: {fact}.{note}", notes=notes_out)


@tool(
    "Look up what is known about something. Substring match on the subject, "
    "then on the fact text.",
    {"subject": s("What to look up. Leave empty to list every subject known.")},
)
def fact_lookup(subject=None):
    if not subject:
        rows = q("SELECT subject, COUNT(*) n, MAX(recorded_at) last FROM facts GROUP BY LOWER(subject) ORDER BY subject")
        if not rows:
            return ok("Nothing recorded yet.", subjects=[])
        return ok("Known subjects: " + ", ".join(r["subject"] for r in rows),
                  subjects=[dict(r) for r in rows])
    like = f"%{subject.lower()}%"
    rows = q("SELECT * FROM facts WHERE LOWER(subject) LIKE ? OR LOWER(fact) LIKE ? ORDER BY id DESC", (like, like))
    if not rows:
        known = [r["subject"] for r in q("SELECT DISTINCT subject FROM facts ORDER BY subject LIMIT 12")]
        raise ToolError(f"Nothing recorded about {subject!r}.", known_subjects=known)
    parts = [f"{r['subject']}: {r['fact']} ({r['recorded_at'][:10]}, {r['recorded_by'] or 'unknown'})"
             for r in rows]
    return ok(" | ".join(parts), facts=[dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Capture
#
# One inbox for anything that arrives without a home. The reason personal
# systems die is not effort, it is the decision of where a thing goes. So:
# capture with no destination, file it later.
# ---------------------------------------------------------------------------


@tool(
    "Drop something into the capture inbox without deciding where it belongs. "
    "Use this for anything arriving unstructured — a text message, a photo "
    "caption, a passing thought.",
    {"raw": s("The text exactly as it arrived."),
     "source": s("Where it came from: 'sms', 'telegram', 'voice', 'email'."),
     "from_person": s("Who sent it, if known.")},
    required=["raw"],
)
def capture_add(raw, source=None, from_person=None):
    raw = (raw or "").strip()
    if not raw:
        raise ToolError("Nothing to capture.")
    notes_out = []
    person = resolve_person(from_person, notes_out) if from_person else None
    cur = write("INSERT INTO capture (raw, source, from_person, created_at) VALUES (?,?,?,?)",
                (raw, source, person, now_iso()))
    pending = q("SELECT COUNT(*) c FROM capture WHERE status='pending'")[0]["c"]
    return ok(f"Captured #{cur.lastrowid}. {pending} pending.", capture_id=cur.lastrowid, notes=notes_out)


@tool(
    "Everything captured but not yet filed. Read these, decide where each one "
    "belongs, act on it with the right tool, then call capture_file.",
    {"limit": i("How many to return.", default=20)},
)
def capture_pending(limit=20):
    rows = q("SELECT * FROM capture WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))
    if not rows:
        return ok("Capture inbox is empty.", items=[])
    parts = [f"#{r['id']} {r['raw']}" + (f" [{r['source']}]" if r["source"] else "") for r in rows]
    return ok(f"{len(rows)} unfiled: " + " | ".join(parts), items=[dict(r) for r in rows])


@tool(
    "Mark a captured item as filed, naming where it went. Call this only after "
    "the thing actually exists somewhere — a captured item marked filed with "
    "nothing behind it is worse than an unfiled one.",
    {"capture_id": i("The capture id."),
     "filed_to": s("Where it ended up: 'task #12', 'shopping: olive oil', 'fact: furnace filter', 'nothing — noise'.")},
    required=["capture_id", "filed_to"],
)
def capture_file(capture_id, filed_to):
    if not q("SELECT 1 FROM capture WHERE id=?", (capture_id,)):
        raise ToolError(f"No capture #{capture_id}.")
    write("UPDATE capture SET status='filed', filed_to=?, filed_at=? WHERE id=?",
          (filed_to, now_iso(), capture_id))
    left = q("SELECT COUNT(*) c FROM capture WHERE status='pending'")[0]["c"]
    return ok(f"Filed #{capture_id} → {filed_to}. {left} still pending.")


# ---------------------------------------------------------------------------
# Journal
#
# What the agent did, and whether it worked. Two payoffs: "why is the
# thermostat at 64" becomes answerable, and a nightly pass over this table is
# how the system notices its own silent failures.
# ---------------------------------------------------------------------------


@tool(
    "Record something the agent did and how it turned out. Log actions that "
    "changed the world or that someone might later ask about — not reads.",
    {"action": s("What was done: 'set_lights', 'sent brief', 'added task'."),
     "target": s("What it was done to: 'office', 'shopping list'."),
     "detail": s("Enough to reconstruct it later, including why."),
     "outcome": s("How it went.", default="ok", enum=["ok", "failed", "partial", "skipped"]),
     "actor": s("Who or what asked for it.")},
    required=["action"],
)
def journal_record(action, target=None, detail=None, outcome="ok", actor=None):
    if outcome not in ("ok", "failed", "partial", "skipped"):
        raise ToolError(f"Unknown outcome {outcome!r}.", known=["ok", "failed", "partial", "skipped"])
    who, _ = actor_note(actor)
    write("INSERT INTO journal (ts, actor, action, target, detail, outcome) VALUES (?,?,?,?,?,?)",
          (now_iso(), who, action, target, detail, outcome))
    return ok(f"Logged: {action}{' → ' + target if target else ''} ({outcome}).")


@tool(
    "Read back what the agent did, failures first. This is the input to the "
    "nightly self-audit: anything that failed or only partly worked is a thing "
    "the house believes happened and did not.",
    {"days": i("How far back.", default=1),
     "only_problems": b("Only failed, partial, and skipped entries.")},
)
def journal_review(days=1, only_problems=False):
    since = (datetime.now() - timedelta(days=max(1, days))).replace(microsecond=0).isoformat(sep=" ")
    sql = "SELECT * FROM journal WHERE ts >= ?"
    args = [since]
    if only_problems:
        sql += " AND outcome != 'ok'"
    rows = q(sql + " ORDER BY outcome='ok', ts DESC", args)
    if not rows:
        return ok(f"Nothing logged in the last {days} day(s)." if not only_problems
                  else f"No failures in the last {days} day(s).", entries=[])
    problems = [r for r in rows if r["outcome"] != "ok"]
    parts = [f"{r['ts'][5:16]} {r['action']}"
             f"{' → ' + r['target'] if r['target'] else ''}"
             f"{'' if r['outcome'] == 'ok' else ' [' + r['outcome'].upper() + ']'}"
             f"{': ' + r['detail'] if r['detail'] else ''}" for r in rows]
    head = f"{len(problems)} problem(s) of {len(rows)} logged actions. " if problems else f"{len(rows)} actions, all clean. "
    return ok(head + " | ".join(parts[:25]),
              entries=[dict(r) for r in rows], problems=[dict(r) for r in problems])


def banner():
    bits = [f"STATE_DB={DB_PATH}", f"STATE_PERSON={DEFAULT_PERSON or 'UNSET (writes attributed to unknown)'}"]
    return "  ".join(bits)


if __name__ == "__main__":
    run("state-mcp", "1.0", banner)
