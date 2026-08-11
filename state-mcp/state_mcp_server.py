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

*Who* is per message, not per process. One bot instance serves the whole
household, so there is no such thing as "the person this server speaks for" -
every write says who asked for it by passing the caller's chat account id:

    shopping_add item="olive oil" actor=389104857203441664

An account nobody has claimed resolves to a provisional `discord:<id>` person
rather than being guessed at or silently credited to whoever configured the
server. `person_link` turns it into a real name and rewrites the history it
already wrote. Writes with no actor at all are recorded as done by the agent
itself, which is honest and visible rather than wrong and invisible.

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
    STATE_AGENT   what to call the agent itself in the record. Default 'hermes'.
                  This is not a person and never joins the roster.

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

# What the agent calls itself in the record. Not a person: it never joins the
# roster and cannot be assigned a task. A write with no actor is the agent's
# own, and saying so is the point - the alternative is crediting it to whoever
# happens to be named in the server's configuration, which in a household bot
# is a coin flip between two people.
AGENT_NAME = os.environ.get("STATE_AGENT", "").strip() or "hermes"

# Read only so state_status can say it is being ignored. It used to be the
# default actor, which is exactly the bug: one value per process, one process
# for the whole house.
LEGACY_PERSON = os.environ.get("STATE_PERSON", "").strip()

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
    name        TEXT PRIMARY KEY,
    aliases     TEXT DEFAULT '',
    provisional INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities (
    platform    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    person      TEXT NOT NULL,
    linked_by   TEXT,
    linked_at   TEXT NOT NULL,
    PRIMARY KEY (platform, external_id)
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
    for_dish  TEXT,
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
    assumed        INTEGER DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_identities_person ON identities(person);
"""

# Columns naming a person. Every one of them has to move when two people turn
# out to be one - a merge that rewrites nine of these and misses the tenth
# leaves records attributed to a name that no longer exists on the roster, and
# nothing reports it because every individual query still returns rows.
ATTRIBUTION = [
    ("tasks", "assignee"), ("tasks", "created_by"), ("tasks", "completed_by"),
    ("shopping", "added_by"), ("shopping", "bought_by"),
    ("pantry", "updated_by"),
    ("meals", "cook"), ("meals", "created_by"),
    ("appointments", "who"), ("appointments", "created_by"),
    ("facts", "recorded_by"),
    ("capture", "from_person"),
    ("journal", "actor"),
]


# Things a kitchen simply has, which a recipe should never put on the shopping
# list. This is the difference between "here are the four things you need for
# carbonara" and a list of nineteen items including salt - and the second one
# gets ignored, which costs you the list itself.
#
# Seeded once, into new databases only, and entirely editable afterwards:
# `pantry_set item=paprika assumed=true` adds one, `assumed=false` drops one
# back to being bought normally. Deliberately short. Something wrongly assumed
# is worse than something wrongly listed, because you find out at the stove.
ASSUMED_STAPLES = [
    "salt", "pepper", "olive oil", "vegetable oil", "vinegar", "flour",
    "sugar", "butter", "water", "ice",
]


def migrate(conn):
    """Bring a database written by an older version up to the current shape.

    The household database is the one thing here that must survive every
    upgrade untouched, so schema changes are additive and applied in place.
    `CREATE TABLE IF NOT EXISTS` covers new tables; a new column on an existing
    table does not, and a missing one fails at the first write rather than at
    startup.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
    if "provisional" not in columns:
        conn.execute("ALTER TABLE people ADD COLUMN provisional INTEGER DEFAULT 0")
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(pantry)")}
    if "assumed" not in columns:
        conn.execute("ALTER TABLE pantry ADD COLUMN assumed INTEGER DEFAULT 0")
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(shopping)")}
    if "for_dish" not in columns:
        conn.execute("ALTER TABLE shopping ADD COLUMN for_dish TEXT")


def seed_assumed_staples(conn):
    """Give a brand-new database a kitchen that already has salt in it.

    New databases only. Seeding an existing one would resurrect items the
    household had deliberately removed, and the first sign of that would be
    salt reappearing on the shopping list months later with nobody able to say
    why.
    """
    for item in ASSUMED_STAPLES:
        conn.execute(
            "INSERT OR IGNORE INTO pantry (item, assumed, staple, qty, updated_by, updated_at) "
            "VALUES (?, 1, 0, 0, ?, ?)", (item, AGENT_NAME, now_iso()))

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
        # Whether this database already existed decides whether it gets seeded,
        # so it has to be answered before the DDL creates the tables.
        fresh = not _conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pantry'").fetchone()
        _conn.executescript(DDL)
        migrate(_conn)
        if fresh:
            seed_assumed_staples(_conn)
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
# One bot serves the whole household, so identity arrives with each message and
# not from the environment. An `actor` is therefore either a name the roster
# knows or a chat account id, and the two need different handling:
#
#   A name that does not match is registered rather than rejected. Rejecting
#   means a new housemate blocks every write until someone runs an admin
#   command; silently accepting free text means "Sam", "sam", and "Smaa" become
#   three people. So register it, flag it provisional, and say so.
#
#   An account id that is not linked is *not* turned into a person called
#   "389104857203441664". It gets a provisional `discord:<id>` row, which reads
#   as what it is, sorts to the top of state_status, and can be folded into a
#   real name later by person_link without losing the writes made in between.
#
# Reads never create anything. lookup_person is the read path and returns None
# for an unknown; resolve_person is the write path and registers. A digest that
# invented a housemate because someone typed a name wrong would be a read with
# a side effect, and those are the ones you never think to look for.
# ---------------------------------------------------------------------------

# Chat account ids: long digit strings. Discord snowflakes are 17-19 digits
# today and grow by one roughly every few years, so the range is generous at
# both ends. No human name is 15 consecutive digits, which is what makes a bare
# id safe to accept without a prefix.
SNOWFLAKE = re.compile(r"^\d{15,22}$")
PLATFORM_PREFIXED = re.compile(r"^([a-z]+):(.+)$", re.I)
PLATFORMS = ["discord", "telegram", "sms", "other"]


def parse_identity(text):
    """Return (platform, external_id) if this names an account, else None.

    Bare digits mean Discord because that is the household's chat surface. An
    id from anywhere else is written `telegram:12345`, which is also the form
    stored for unlinked accounts, so feeding a summary back in resolves.
    """
    text = str(text).strip()
    match = PLATFORM_PREFIXED.match(text)
    if match and match.group(1).lower() in PLATFORMS:
        return match.group(1).lower(), match.group(2).strip()
    if SNOWFLAKE.match(text):
        return "discord", text
    return None


def identity_label(platform, external_id):
    return f"{platform}:{external_id}"


def person_for_identity(platform, external_id):
    rows = q("SELECT person FROM identities WHERE platform=? AND external_id=?",
             (platform, external_id))
    return rows[0]["person"] if rows else None


def lookup_person(name):
    """Resolve a name or account id for a *read*. Never writes. None if unknown."""
    if name is None or not str(name).strip():
        return None
    name = str(name).strip()
    if name.lower() == AGENT_NAME.lower():
        return AGENT_NAME
    identity = parse_identity(name)
    if identity:
        linked = person_for_identity(*identity)
        if linked:
            return linked
        label = identity_label(*identity)
        return label if q("SELECT 1 FROM people WHERE name=?", (label,)) else None
    lowered = name.lower()
    for row in q("SELECT name, aliases FROM people"):
        if row["name"].lower() == lowered:
            return row["name"]
        if lowered in [a.strip().lower() for a in (row["aliases"] or "").split(",") if a.strip()]:
            return row["name"]
    return None


def resolve_person(name, note=None):
    """Resolve for a *write*, registering whatever does not already exist."""
    if name is None or not str(name).strip():
        return AGENT_NAME
    name = str(name).strip()
    if name.lower() == AGENT_NAME.lower():
        return AGENT_NAME

    identity = parse_identity(name)
    if identity:
        platform, external_id = identity
        linked = person_for_identity(platform, external_id)
        if linked:
            return linked
        label = identity_label(platform, external_id)
        write("INSERT OR IGNORE INTO people (name, provisional, created_at) VALUES (?, 1, ?)",
              (label, now_iso()))
        if note is not None:
            note.append(
                f"No one is linked to {label}. Ask whose account it is, then call "
                f"person_link name=<their name> discord_id={external_id} — that "
                "also re-attributes everything they have written so far. Until "
                "then their records carry the raw account id."
            )
        return label

    known = lookup_person(name)
    if known:
        return known
    write("INSERT OR IGNORE INTO people (name, provisional, created_at) VALUES (?, 1, ?)",
          (name, now_iso()))
    if note is not None:
        note.append(
            f"{name!r} was not on the roster and has been added provisionally. "
            "If it is a typo or another name for someone already here, fix it "
            "with person_merge before it spreads."
        )
    return name


def actor_note(actor):
    """Resolve an actor and collect anything worth saying about it."""
    notes = []
    if actor is None or not str(actor).strip():
        notes.append(
            f"No actor was given, so this is recorded as done by {AGENT_NAME} "
            "itself. If a person asked for it, pass actor=<their Discord user id>."
        )
        return AGENT_NAME, notes
    return resolve_person(actor, notes), notes


def rename_everywhere(old, new):
    """Point every person-shaped column at `new`. Caller owns the transaction."""
    touched = 0
    for table, column in ATTRIBUTION:
        # Table and column names are the module-level constant, never input.
        touched += write(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                         (new, old)).rowcount
    return touched


# ---------------------------------------------------------------------------
# Ingredients
#
# A recipe line is "2 tbsp extra virgin olive oil, finely drizzled" and the
# pantry says "olive oil". Matching those is the whole job: get it wrong in one
# direction and the list grows salt and pepper every time anyone cooks, get it
# wrong in the other and you arrive at the stove without the olive oil.
#
# So: strip the measurement and the preparation, then match on whole words. The
# pantry item's words must all appear in the ingredient, which lets "olive oil"
# match "extra virgin olive oil" while keeping "sesame oil" from matching it.
# ---------------------------------------------------------------------------

UNITS = [
    "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "cup", "cups", "g", "gram", "grams", "kg", "ml", "l", "litre", "litres",
    "liter", "liters", "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds",
    "clove", "cloves", "can", "cans", "tin", "tins", "jar", "jars", "packet",
    "packets", "pack", "packs", "bunch", "bunches", "pinch", "pinches", "dash",
    "handful", "handfuls", "slice", "slices", "sprig", "sprigs", "stick",
    "sticks", "head", "heads", "piece", "pieces", "large", "medium", "small",
]
QUANTITY = re.compile(
    r"^\s*(?:[\d]+(?:[./]\d+)?|[½¼¾⅓⅔⅛]|a|an|one|two|three|four|five|six)"
    r"(?:\s*[-–to]+\s*[\d]+(?:[./]\d+)?)?\s*", re.I)
PARENTHETICAL = re.compile(r"\([^)]*\)")
# Words that say nothing about what to buy. "chopped" and "minced" are here
# because they describe what you do to the onion after you get home. "ground",
# "diced" and "grated" are deliberately *not*: ground beef, diced tomatoes and
# grated parmesan are things you buy as such, and flattening them to beef,
# tomatoes and parmesan sends someone to the wrong shelf.
NOISE_WORDS = {"of", "fresh", "freshly", "finely", "roughly", "coarsely",
               "chopped", "sliced", "minced",
               "optional", "to", "taste", "plus", "more", "for", "serving",
               "garnish", "good", "quality", "free", "range", "organic"}


def normalize_ingredient(text):
    """A recipe line reduced to the thing you would look for in a cupboard."""
    text = PARENTHETICAL.sub(" ", str(text or "").lower())
    text = text.split(",")[0]           # "onion, finely chopped" -> "onion"
    text = re.sub(r"[^a-z0-9½¼¾⅓⅔⅛/.\s-]", " ", text)
    previous = None
    while previous != text:             # "1 1/2 cups" needs two passes
        previous = text
        text = QUANTITY.sub("", text)
        words = text.split()
        if words and words[0] in UNITS:
            text = " ".join(words[1:])
    words = [w for w in text.split() if w not in NOISE_WORDS]
    return " ".join(words).strip()


# Words that describe a thing without changing what it is. Everything else in
# front of a pantry item makes it a *different* product: "extra virgin" olive
# oil is olive oil, "rice" vinegar is not vinegar, and "peanut" butter is not
# butter. Without this distinction the pantry quietly absorbs every ingredient
# that happens to end in a word it stocks.
QUALIFIERS = {
    # "ground" is deliberately absent. It reads like a qualifier and behaves
    # like one for pepper, but ground beef is not beef and ground coffee is not
    # coffee - and a wrong skip is silent where a wrong listing is one extra
    # line someone reads in the shop.
    "extra", "virgin", "sea", "kosher", "table", "black", "white", "fine",
    "coarse", "whole", "plain", "all-purpose", "allpurpose", "light",
    "dark", "unsalted", "salted", "granulated", "caster", "raw", "pure",
    "cooking", "vegetable", "warm", "cold", "hot", "boiling", "lukewarm",
}


def ingredient_matches(pantry_item, ingredient):
    """True when the pantry item covers the ingredient.

    Two conditions, and the second is the one that took a bug to find. The
    pantry item must be a whole-word *suffix* of the ingredient, because English
    puts the head noun last. And every word in front of it must be a qualifier
    rather than a distinguishing noun - otherwise "vinegar" covers "rice
    vinegar", "butter" covers "peanut butter", and the shopping list silently
    drops the one ingredient the dish was actually about.

    Suffix alone was the first attempt. It reads as obviously correct and is
    wrong for exactly the ingredients worth getting right.
    """
    have = normalize_ingredient(pantry_item).split()
    want = ingredient.split()
    if not have or len(have) > len(want) or want[-len(have):] != have:
        return False
    return all(word in QUALIFIERS for word in want[:len(want) - len(have)])


def actor_field(desc):
    """An `actor` parameter.

    What an actor may *be* is identical everywhere and worth stating in every
    schema, because the agent reads one tool's parameters at the moment it
    calls it and not the server's docstring. What the actor *did* differs per
    tool and is the part worth writing by hand.
    """
    return s(desc + " Pass their Discord user id; a roster name also works.")


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
        # Counted separately: one is stock, the other is a standing decision
        # never to shop for something. Adding them together makes a household
        # that tracks nothing look like one that tracks nine things.
        "pantry items": q("SELECT COUNT(*) c FROM pantry WHERE NOT (assumed=1 AND staple=0)")[0]["c"],
        "assumed always in stock": q("SELECT COUNT(*) c FROM pantry WHERE assumed=1")[0]["c"],
        "meals planned": q("SELECT COUNT(*) c FROM meals WHERE date >= ?", (today().isoformat(),))[0]["c"],
        "facts": q("SELECT COUNT(*) c FROM facts")[0]["c"],
        "pending captures": q("SELECT COUNT(*) c FROM capture WHERE status='pending'")[0]["c"],
    }
    people = [r["name"] for r in q("SELECT name FROM people WHERE provisional=0 ORDER BY name")]
    unresolved = [r["name"] for r in q("SELECT name FROM people WHERE provisional=1 ORDER BY name")]
    linked = q("SELECT COUNT(*) c FROM identities")[0]["c"]
    counts["linked accounts"] = linked

    warnings = []
    if unresolved:
        warnings.append(
            "Not attributed to a real person: " + ", ".join(unresolved) + ". "
            "Anything shaped 'discord:<id>' is someone who has written to the "
            "house without being introduced — person_link names them and moves "
            "their records. Anything else is probably a typo — person_merge."
        )
    if not linked and people:
        warnings.append(
            "Nobody on the roster has a Discord account linked, so messages "
            "cannot be attributed by id. person_link fixes this once per person."
        )
    if LEGACY_PERSON:
        warnings.append(
            f"STATE_PERSON={LEGACY_PERSON!r} is set and is ignored. One bot "
            "serves the whole household, so a single default person meant every "
            "unattributed write was credited to one of them regardless of who "
            "was talking. Remove it from the MCP config; pass actor=<Discord "
            "user id> per call instead."
        )
    if not os.access(DB_PATH, os.W_OK):
        warnings.append(f"{DB_PATH} is not writable. Every write tool will fail.")
    return ok(
        f"State store at {DB_PATH}. " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".",
        database=DB_PATH, counts=counts, people=people, unresolved=unresolved,
        agent_name=AGENT_NAME, warnings=warnings,
    )


def check_not_agent(name):
    if name.lower() == AGENT_NAME.lower():
        raise ToolError(
            f"{AGENT_NAME!r} is the agent's own name in the record, not a member "
            "of the household. Pick a different name, or rename the agent with "
            "the STATE_AGENT environment variable.")


@tool(
    "Add a person to the household roster, with optional spoken aliases. Use "
    "person_link instead when you know their Discord id — it does this and "
    "connects the account in one step.",
    {"name": s("Canonical name, as it should be displayed."),
     "aliases": s("Comma-separated other names this person is called.")},
    required=["name"],
)
def person_add(name, aliases=""):
    name = (name or "").strip()
    if not name:
        raise ToolError("A person needs a name.")
    check_not_agent(name)
    if parse_identity(name):
        raise ToolError(
            f"{name!r} is an account id, not a name. Use person_link to attach "
            "an account to a person.")
    write(
        "INSERT INTO people (name, aliases, provisional, created_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(name) DO UPDATE SET aliases=excluded.aliases, provisional=0",
        (name, aliases or "", now_iso()),
    )
    return ok(f"{name} is on the household roster." + (f" Also known as: {aliases}." if aliases else ""))


@tool(
    "Connect a Discord account to a person, so everything they say from then on "
    "is attributed to them by name. Call this the first time someone new talks "
    "to you, after asking what to call them. It also re-attributes anything "
    "already written under the raw account id, so nothing is lost by linking "
    "late.",
    {"name": s("What to call them. Creates the person if they are not on the roster."),
     "discord_id": s("Their Discord user id — the long number, not their handle."),
     "aliases": s("Comma-separated other names they are called."),
     "actor": actor_field("Who is setting this up.")},
    required=["name", "discord_id"],
)
def person_link(name, discord_id, aliases=None, actor=None):
    name = (name or "").strip()
    if not name:
        raise ToolError("A person needs a name.")
    check_not_agent(name)
    external_id = str(discord_id).strip()
    if not SNOWFLAKE.match(external_id):
        raise ToolError(
            f"{external_id!r} is not a Discord user id. It is 15-22 digits and "
            "comes from the message, not from typing out their display name.")
    if parse_identity(name):
        raise ToolError(
            "name is the person's name, discord_id is the number. Those look "
            "swapped.")

    who, notes_out = actor_note(actor)
    label = identity_label("discord", external_id)
    previous = person_for_identity("discord", external_id)
    if previous == name:
        return ok(f"{name} is already linked to that Discord account.", person=name)

    moved = 0
    with transaction():
        write("INSERT INTO people (name, aliases, provisional, created_at) VALUES (?,?,0,?) "
              "ON CONFLICT(name) DO UPDATE SET provisional=0" +
              (", aliases=excluded.aliases" if aliases else ""),
              (name, aliases or "", now_iso()))
        write("INSERT INTO identities (platform, external_id, person, linked_by, linked_at) "
              "VALUES ('discord',?,?,?,?) ON CONFLICT(platform, external_id) DO UPDATE SET "
              "person=excluded.person, linked_by=excluded.linked_by, linked_at=excluded.linked_at",
              (external_id, name, who, now_iso()))
        # Anything written before the link carries the raw account id. Moving it
        # now is the whole reason linking late is safe: the shopping item they
        # added this morning becomes theirs rather than staying orphaned under a
        # number nobody recognises.
        if q("SELECT 1 FROM people WHERE name=? AND provisional=1", (label,)):
            moved = rename_everywhere(label, name)
            write("DELETE FROM people WHERE name=? AND provisional=1", (label,))

    summary = f"{label} is {name}."
    if moved:
        summary += f" {moved} earlier record{'s' if moved != 1 else ''} re-attributed to them."
    if previous and previous != name:
        summary += (f" That account was previously linked to {previous}, whose "
                    "existing records were left alone.")
        notes_out.append(
            f"The Discord account now points at {name} but {previous}'s past "
            f"records still say {previous}. If they are the same person, run "
            f"person_merge from_person={previous} into={name}.")
    return ok(summary, person=name, discord_id=external_id,
              reattributed=moved, notes=notes_out)


@tool(
    "Who is this Discord account? Call it when someone starts talking and you "
    "are not sure you know them — the answer says whether to use their name or "
    "to ask for it.",
    {"discord_id": s("The Discord user id from the message.")},
    required=["discord_id"],
)
def person_identify(discord_id):
    external_id = str(discord_id).strip()
    if not SNOWFLAKE.match(external_id):
        raise ToolError(f"{external_id!r} is not a Discord user id (15-22 digits).")
    person = person_for_identity("discord", external_id)
    if person:
        row = q("SELECT aliases FROM people WHERE name=?", (person,))
        aliases = row[0]["aliases"] if row else ""
        return ok(f"That is {person}." + (f" Also known as: {aliases}." if aliases else ""),
                  person=person, linked=True, aliases=aliases or None)
    roster = [r["name"] for r in q("SELECT name FROM people WHERE provisional=0 ORDER BY name")]
    return ok(
        f"Discord account {external_id} is not linked to anyone. Ask what to "
        f"call them and run person_link name=<their name> discord_id={external_id}. "
        "You can keep working meanwhile — pass the id as the actor and it will "
        "be re-attributed when you link it.",
        person=None, linked=False, discord_id=external_id, roster=roster)


@tool(
    "Fold one person's records into another. This is the fix for a typo, a "
    "nickname that became its own person, or the same human arriving twice — "
    "everything attributed to `from_person` becomes `into`, and any account "
    "linked to them follows.",
    {"from_person": s("The name to retire. May be a raw 'discord:<id>' entry."),
     "into": s("The name to keep."),
     "actor": actor_field("Who is making the correction.")},
    required=["from_person", "into"],
)
def person_merge(from_person, into, actor=None):
    source = (from_person or "").strip()
    target = (into or "").strip()
    if not source or not target:
        raise ToolError("Name both the person to retire and the one to keep.")
    if source.lower() == target.lower():
        raise ToolError("Those are the same name — nothing to merge.")
    check_not_agent(source)
    check_not_agent(target)

    found = lookup_person(source) or source
    if not q("SELECT 1 FROM people WHERE name=?", (found,)):
        # An account with no records behind it is nothing to merge *from* - the
        # caller means "this account is Sarah", which is what person_link says.
        identity = parse_identity(source)
        if identity:
            raise ToolError(
                f"{source} has no records here, so there is nothing to fold into "
                f"{target}. If you mean that account belongs to {target}, that is "
                f"person_link name={target} discord_id={identity[1]}.")
        roster = [r["name"] for r in q("SELECT name FROM people ORDER BY name LIMIT 12")]
        raise ToolError(f"Nobody on the roster called {source!r}.", roster=roster)
    keeper = lookup_person(target) or target
    if found == keeper:
        raise ToolError(f"{source!r} and {target!r} already resolve to the same person, {found}.")

    who, notes_out = actor_note(actor)
    with transaction():
        write("INSERT INTO people (name, provisional, created_at) VALUES (?,0,?) "
              "ON CONFLICT(name) DO UPDATE SET provisional=0", (keeper, now_iso()))
        moved = rename_everywhere(found, keeper)
        # Accounts move too, or the next message from them recreates the row we
        # are deleting and the merge silently undoes itself.
        write("UPDATE identities SET person=? WHERE person=?", (keeper, found))
        # A provisional 'discord:<id>' row is an account that was never linked,
        # so there is no identity row to move - only the label. Claiming it here
        # is what makes merging away a placeholder stick. Without this the next
        # message from that account rebuilds the placeholder and the merge has
        # to be done again, forever.
        placeholder = parse_identity(found)
        if placeholder and not person_for_identity(*placeholder):
            write("INSERT INTO identities (platform, external_id, person, linked_by, linked_at) "
                  "VALUES (?,?,?,?,?)", (placeholder[0], placeholder[1], keeper, who, now_iso()))
        write("DELETE FROM people WHERE name=?", (found,))
        write("INSERT INTO journal (ts, actor, action, target, detail, outcome) "
              "VALUES (?,?,?,?,?,?)",
              (now_iso(), who, "person_merge", keeper,
               f"{found} folded into {keeper}, {moved} records moved", "ok"))
    return ok(f"{found} is now {keeper}. {moved} record{'s' if moved != 1 else ''} moved.",
              person=keeper, retired=found, moved=moved, notes=notes_out)


@tool("List everyone on the household roster, the names each answers to, and "
      "which chat accounts are connected to whom.")
def people_list():
    rows = q("SELECT name, aliases, provisional FROM people ORDER BY provisional, name")
    if not rows:
        return ok("Nobody on the roster yet. Link people with person_link so "
                  "writes can be attributed by name.", people=[], unresolved=[])
    links = {}
    for row in q("SELECT platform, external_id, person FROM identities"):
        links.setdefault(row["person"], []).append(identity_label(row["platform"], row["external_id"]))
    known = [r for r in rows if not r["provisional"]]
    unresolved = [r["name"] for r in rows if r["provisional"]]

    parts = []
    for row in known:
        extra = [x for x in (row["aliases"] or "", ", ".join(links.get(row["name"], []))) if x]
        parts.append(row["name"] + (f" ({'; '.join(extra)})" if extra else ""))
    summary = ("Household: " + ", ".join(parts) + "." if parts
               else "Nobody confirmed on the roster yet.")
    if unresolved:
        summary += (f" Not yet identified: {', '.join(unresolved)} — "
                    "person_link or person_merge will resolve these.")
    return ok(summary,
              people=[dict(r) for r in known],
              identities=links,
              unresolved=unresolved)


# ---------------------------------------------------------------------------
# The one-call view
# ---------------------------------------------------------------------------


@tool(
    "The whole household in one call: what is due, what is planned, what is "
    "unfiled. Use this to answer 'what's going on' and to build the daily "
    "brief. Anything unusual comes first.",
    {"person": s("Optional. Lead with this person's items — their Discord user "
                  "id or roster name — but still show everything."),
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

    mine, notes_out = [], []
    if person:
        # A read, so an unrecognised name is reported rather than registered.
        # The digest is the most-called tool here; inventing a housemate every
        # time someone's name is typed wrong would fill the roster from the one
        # place nobody would think to look.
        who = lookup_person(person)
        if who is None:
            notes_out.append(
                f"Nobody here is called {person!r}, so the rest of this is the "
                "whole household. Check people_list, or link their Discord "
                "account with person_link.")
        else:
            mine = [dict(r) for r in q(
                "SELECT * FROM tasks WHERE status='open' AND assignee=? ORDER BY due IS NULL, due",
                (who,))]
            if mine:
                lines.insert(0, f"{who}: " + "; ".join(
                    f"{t['title']}{' (' + human_date(t['due']) + ')' if t['due'] else ''}"
                    for t in mine[:4]))

    headline = "Household — nothing needs attention." if not (overdue or unfiled or low) else "Household"
    return ok(
        headline + "\n  " + "\n  ".join(lines) if lines else headline,
        needs_attention=bool(overdue or unfiled or low),
        notes=notes_out,
        overdue=[dict(r) for r in overdue],
        due_today=[dict(r) for r in due_now],
        for_person=mine,
        low_staples=[dict(r) for r in low],
        shopping=[dict(r) for r in shopping],
        appointments=[dict(r) for r in appts],
        meals=[dict(r) for r in meals],
        unfiled_captures=unfiled,
    )


@tool(
    "What the household actually got done, and who did it. Answers 'who did "
    "what this week' — chores finished, shopping bought, meals cooked. This is "
    "the counterpart to household_digest: that one is what is still outstanding, "
    "this one is what is behind you.",
    {"days": i("How far back to look.", default=7),
     "person": s("Only this person's doing. Discord user id or roster name."),
     "include_agent": b("Include what the agent did on its own. Off by default, "
                        "because 'who did what' is a question about people.")},
)
def household_history(days=7, person=None, include_agent=False):
    days = max(1, days)
    since = (today() - timedelta(days=days)).isoformat()
    notes_out = []
    who = None
    if person:
        who = lookup_person(person)  # a read: never registers a new person
        if who is None:
            notes_out.append(f"Nobody here is called {person!r}. Check people_list.")
            who = person

    def restrict(column, rows):
        if who:
            rows = [r for r in rows if r[column] == who]
        elif not include_agent:
            rows = [r for r in rows if r[column] != AGENT_NAME]
        return rows

    # completed_at is a timestamp and `since` a date; comparing them as strings
    # works because both are ISO and the date sorts before any time on that day.
    tasks = restrict("completed_by", q(
        "SELECT * FROM tasks WHERE status='done' AND completed_at >= ? "
        "ORDER BY completed_at DESC, id DESC", (since,)))
    dropped = restrict("completed_by", q(
        "SELECT * FROM tasks WHERE status='dropped' AND completed_at >= ? "
        "ORDER BY completed_at DESC, id DESC", (since,)))
    bought = restrict("bought_by", q(
        "SELECT * FROM shopping WHERE status='bought' AND bought_at >= ? "
        "ORDER BY bought_at DESC, id DESC", (since,)))
    # Meals are a plan, not a receipt - nothing here confirms anyone ate it. So
    # this is "what was on the plan for those days", and the summary says so
    # rather than claiming a dinner happened.
    meals = restrict("cook", q(
        "SELECT * FROM meals WHERE date >= ? AND date <= ? ORDER BY date DESC",
        (since, today().isoformat())))

    by_person = {}
    for row, verb in ([(r, "chores") for r in tasks] + [(r, "shopping") for r in bought]
                      + [(r, "meals") for r in meals]):
        name = row["completed_by"] if verb == "chores" else (
            row["bought_by"] if verb == "shopping" else row["cook"])
        if name:
            by_person.setdefault(name, {"chores": 0, "shopping": 0, "meals": 0})[verb] += 1

    lines = []
    if tasks:
        lines.append("chores done: " + "; ".join(
            f"{r['title']} ({r['completed_by'] or 'unknown'}, {human_date(r['completed_at'][:10])})"
            for r in tasks[:10]))
    if meals:
        lines.append("meals planned: " + "; ".join(
            f"{human_date(r['date'])} {r['dish']}" + (f" ({r['cook']})" if r["cook"] else "")
            for r in meals[:10]))
    if bought:
        shoppers = {}
        for r in bought:
            shoppers.setdefault(r["bought_by"] or "unknown", []).append(r["item"])
        lines.append("shopping: " + "; ".join(
            f"{name} bought {len(items)} ({', '.join(items[:5])})"
            for name, items in shoppers.items()))
    if dropped:
        lines.append("dropped: " + "; ".join(
            f"{r['title']} ({r['completed_by'] or 'unknown'})" for r in dropped[:5]))

    window = f"last {days} day{'s' if days != 1 else ''}"
    if not lines:
        return ok(f"Nothing recorded as done in the {window}"
                  + (f" by {who}." if who else "."),
                  chores=[], meals=[], shopping=[], dropped=[], by_person={},
                  notes=notes_out)
    tally = ", ".join(
        f"{name}: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n)
        for name, counts in sorted(by_person.items()))
    head = f"{who}, {window}" if who else f"The house, {window}"
    return ok(f"{head} — " + "\n  ".join(lines) + (f"\n  totals — {tally}" if tally and not who else ""),
              chores=[dict(r) for r in tasks], meals=[dict(r) for r in meals],
              shopping=[dict(r) for r in bought], dropped=[dict(r) for r in dropped],
              by_person=by_person, days=days, notes=notes_out)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@tool(
    "Add a household task. Anyone can add; everyone sees it.",
    {"title": s("What needs doing, as a short imperative."),
     "area": s("Which part of household life this belongs to.", enum=AREAS),
     "assignee": actor_field("Who is responsible. Leave empty for unassigned."),
     "due": s("YYYY-MM-DD, 'today', 'tomorrow', a weekday, or '+3 days'."),
     "recurrence": s("'weekly', 'quarterly', 'every 3 months'. Recurs from when it is completed, not from a fixed calendar."),
     "notes": s("Anything worth remembering when the time comes."),
     "actor": actor_field("Who is adding this. Omit only when nobody asked "
                          "and you are acting on your own.")},
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
    {"person": s("Only this person's tasks, plus unassigned ones. Discord user "
                  "id or roster name."),
     "area": s("Only this area.", enum=AREAS),
     "window": s("'overdue', 'today', 'week', or 'all'.", default="all",
                 enum=["overdue", "today", "week", "all"]),
     "include_done": b("Include recently completed tasks.")},
)
def task_list(person=None, area=None, window="all", include_done=False):
    sql = "SELECT * FROM tasks WHERE status = ?"
    args = ["open"]
    notes_out = []
    if person:
        who = lookup_person(person)  # a read: never registers a new person
        if who is None:
            notes_out.append(f"Nobody here is called {person!r}, so this is "
                             "only the unassigned tasks.")
            who = person
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
                  tasks=[], completed=[dict(r) for r in done], notes=notes_out)
    parts = []
    for r in rows:
        line = f"#{r['id']} {r['title']}"
        extra = [x for x in (human_date(r["due"]) if r["due"] else "", r["assignee"] or "", r["area"]) if x]
        parts.append(line + (f" ({', '.join(extra)})" if extra else ""))
    return ok(f"{len(rows)} open — " + "; ".join(parts),
              tasks=[dict(r) for r in rows], completed=[dict(r) for r in done],
              notes=notes_out)


@tool(
    "Mark a task done. If it recurs, the next one is created from today, not "
    "from the old due date — a filter changed late is due three months from "
    "when it was changed.",
    {"task_id": i("The id from task_add or task_list."),
     "actor": actor_field("Who did it — them, not you."),
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
     "assignee": actor_field("Who is responsible now. Pass 'none' to unassign."),
     "due": s("New due date, or 'none' to clear it."),
     "recurrence": s("New recurrence, or 'none' to stop it repeating."),
     "notes": s("Replacement notes."),
     "actor": actor_field("Who is making the change.")},
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
     "actor": actor_field("Who dropped it.")},
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
     "store": s("Where, if it matters."), "actor": actor_field("Who added it.")},
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
    # Why something is on the list is the question asked in the shop, in front
    # of the shelf, about the one item nobody remembers adding. But a recipe
    # adds seven items at once, and repeating "for chicken chili" seven times
    # buries the seven things you actually came to read.
    loose, by_dish = [], {}
    for r in rows:
        label = r["item"] + (f" ({r['qty']})" if r["qty"] else "")
        if r["for_dish"]:
            by_dish.setdefault(r["for_dish"], []).append(label)
        else:
            loose.append(label)
    parts = ([", ".join(loose)] if loose else []) + [
        f"for {dish}: {', '.join(items)}" for dish, items in by_dish.items()]
    return ok(f"{len(rows)} to buy — " + "; ".join(parts), items=[dict(r) for r in rows])


@tool(
    "Mark shopping items bought. Pass items as a comma-separated list, or "
    "leave empty with all=true to clear the whole list after a shop.",
    {"items": s("Comma-separated item names."), "all": b("Mark everything bought."),
     "actor": actor_field("Who did the shopping.")},
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
     "actor": actor_field("Who is removing it.")},
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
    "Work out what a recipe actually needs buying, and put that on the shopping "
    "list. Pass every ingredient the recipe calls for, measurements and all — "
    "this is the tool that decides which of them the house already has, so it "
    "needs the whole list, not your guess at the missing part. Works the same "
    "whether you invented the recipe or someone sent you one.",
    {"ingredients": s("Every ingredient, one per line or separated by semicolons. "
                      "Quantities and prep notes are fine: '2 tbsp olive oil; "
                      "400g spaghetti; 3 cloves garlic, minced'."),
     "dish": s("What it is for. Recorded against each item so the list says why."),
     "preview": b("Work it out and report, but add nothing. Use for 'do we have "
                  "everything for carbonara?'"),
     "actor": actor_field("Who is cooking this.")},
    required=["ingredients"],
)
def shopping_add_recipe(ingredients, dish=None, preview=False, actor=None):
    # " and " splits too: "salt and pepper to taste" is two ingredients on one
    # line, and treated as one it matches neither.
    raw = [part.strip() for line in re.split(r"[;\n]+", str(ingredients or ""))
           for part in re.split(r"\s+and\s+", line) if part.strip()]
    if not raw:
        raise ToolError("No ingredients given. Pass the recipe's ingredient list, "
                        "one per line or separated by semicolons.")
    who, notes_out = actor_note(actor)

    pantry = q("SELECT item, qty, unit, staple, threshold, assumed FROM pantry")
    on_list = {r["item"].lower(): r for r in q("SELECT * FROM shopping WHERE status='needed'")}

    need, assumed, stocked, already, seen = [], [], [], [], set()
    for line in raw:
        name = normalize_ingredient(line)
        if not name or name in seen:
            continue
        seen.add(name)
        if name in on_list:
            already.append(name)
            continue
        # Most specific wins. With both "oil" and "olive oil" in the pantry,
        # "olive oil" is the row that knows whether there is any left.
        matches = sorted((p for p in pantry if ingredient_matches(p["item"], name)),
                         key=lambda p: len(p["item"].split()), reverse=True)
        match = matches[0] if matches else None
        if match is None:
            need.append(name)
        elif match["staple"] and (match["qty"] or 0) <= (match["threshold"] or 0):
            # Counted, and counted as gone. A real measurement outranks an
            # assumption, even when the same row carries both.
            need.append(name)
        elif match["assumed"]:
            assumed.append(name)
        elif (match["qty"] or 0) <= 0:
            # A pantry row is not the same as having any. Treating one at zero
            # as stock is how you get to the stove without the thing you
            # carefully recorded running out of.
            need.append(name)
        else:
            stocked.append(f"{name} ({match['qty']:g}{' ' + match['unit'] if match['unit'] else ''})")

    if not preview:
        with transaction():
            for item in need:
                write("INSERT INTO shopping (item, for_dish, added_by, added_at) "
                      "VALUES (?,?,?,?)", (item, dish, who, now_iso()))

    verb = "Would add" if preview else "Added"
    summary = (f"{verb} {len(need)}: " + ", ".join(need)) if need else \
        ("Nothing to buy" + (f" — you have everything for {dish}." if dish else "."))
    if already:
        summary += f". Already on the list: {', '.join(already)}"
    if stocked:
        summary += f". In the pantry: {', '.join(stocked)}"
    if assumed:
        # Never silent. This is the list's own blind spot, and the person
        # reading it is the only one who can catch "peanut butter" being
        # skipped because the kitchen has butter.
        summary += f". Assumed you have: {', '.join(assumed)}"
    return ok(summary.strip(". ") + ".", dish=dish, to_buy=need, assumed=assumed,
              in_pantry=stocked, already_listed=already, preview=bool(preview),
              notes=notes_out)


@tool(
    "Set what is in the pantry. Staples are the things worth reordering "
    "automatically when they run low.",
    {"item": s("What it is."), "qty": s("How many/much. Numeric."),
     "location": s("Where it lives: pantry, freezer, garage."),
     "unit": s("Optional unit for display: 'bottles', 'kg'."),
     "staple": b("Track this and warn when it drops to the threshold."),
     "threshold": s("Warn at or below this quantity. Numeric. Default 1."),
     "assumed": b("The house always has this, so a recipe should never put it "
                  "on the shopping list. For salt, pepper, oil — the things "
                  "nobody counts. Pass assumed=false to stop assuming it."),
     "actor": actor_field("Who is recording this.")},
    required=["item"],
)
def pantry_set(item, qty=None, location=None, unit=None, staple=False, threshold=None,
               assumed=None, actor=None):
    item = (item or "").strip()
    if not item:
        raise ToolError("Nothing named.")
    who, notes_out = actor_note(actor)
    try:
        qty_v = float(qty) if qty is not None and str(qty).strip() != "" else 0.0
        thr_v = float(threshold) if threshold is not None and str(threshold).strip() != "" else 1.0
    except ValueError:
        raise ToolError("qty and threshold must be numbers. Use the unit field for 'bottles' or 'kg'.")
    # Unlike the rest, `assumed` is left alone when not passed. Every other call
    # to this tool is "here is the current state of this item", but assuming is
    # a standing decision about the item, not an observation of it - and setting
    # a quantity should not silently cancel it.
    prior = q("SELECT assumed FROM pantry WHERE item=?", (item,))
    assumed_v = (prior[0]["assumed"] if prior else 0) if assumed is None else (1 if assumed else 0)
    write(
        "INSERT INTO pantry (item, location, qty, unit, staple, threshold, assumed, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(item) DO UPDATE SET "
        "location=COALESCE(excluded.location, pantry.location), qty=excluded.qty, "
        "unit=COALESCE(excluded.unit, pantry.unit), staple=excluded.staple, "
        "threshold=excluded.threshold, assumed=excluded.assumed, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (item, location, qty_v, unit, 1 if staple else 0, thr_v, assumed_v, who, now_iso()),
    )
    warn = " Below threshold — worth adding to the shopping list." if staple and qty_v <= thr_v else ""
    if assumed_v:
        warn += " Assumed always in stock, so recipes will not list it."
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
        # Assumed-only rows are a standing decision not to list something, not
        # stock. Offering them as "here is what is in the pantry" would suggest
        # deleting salt to fix a typo about flour.
        known = [r["item"] for r in q(
            "SELECT item FROM pantry WHERE NOT (assumed=1 AND staple=0) ORDER BY item LIMIT 15")]
        raise ToolError(f"Nothing in the pantry called {item!r}.", in_pantry=known)
    row = rows[0]
    who, notes_out = actor_note(actor)
    write("DELETE FROM pantry WHERE LOWER(item)=?", (item.lower(),))
    warn = ""
    if row["staple"]:
        warn = " It was a staple, so nothing will warn when it runs low any more."
    if row["assumed"]:
        warn += (" It was assumed always in stock, so recipes will start putting "
                 "it on the shopping list.")
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
     "cook": actor_field("Who is cooking."), "recipe_ref": s("URL, book and page, or a note about where it came from."),
     "notes": s("Anything else — 'needs thawing', 'doubles well'."),
     "actor": actor_field("Who is planning it.")},
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
     "time": s("Local time as HH:MM, if it has one."), "who": actor_field("Who it concerns."),
     "place": s("Where."), "notes": s("Anything to bring or know."),
     "actor": actor_field("Who is recording it.")},
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
    notes_out = []
    if who:
        found = lookup_person(who)  # a read: never registers a new person
        if found is None:
            notes_out.append(f"Nobody here is called {who!r}. Check people_list.")
            found = who
        sql += " AND who = ?"
        args.append(found)
    rows = q(sql + " ORDER BY date, time", args)
    if not rows:
        return ok(f"Nothing on the household calendar in the next {days} days.",
                  appointments=[], notes=notes_out)
    parts = [f"{human_date(r['date'])}{' ' + r['time'] if r['time'] else ''} {r['what']}"
             f"{' (' + r['who'] + ')' if r['who'] else ''}" for r in rows]
    return ok("; ".join(parts), appointments=[dict(r) for r in rows], notes=notes_out)


@tool(
    "Cancel a household appointment. Deleted rather than marked cancelled: an "
    "appointment nobody is going to is noise on a calendar everyone reads.",
    {"appointment_id": i("The id from appointment_add or appointment_list."),
     "reason": s("Why, if it is worth recording."),
     "actor": actor_field("Who cancelled it.")},
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
     "actor": actor_field("Who recorded it.")},
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
     "from_person": actor_field("Who sent it, if known.")},
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
     "actor": actor_field("Who or what asked for it.")},
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
    bits = [f"STATE_DB={DB_PATH}", f"STATE_AGENT={AGENT_NAME}"]
    if LEGACY_PERSON:
        bits.append(f"STATE_PERSON={LEGACY_PERSON} (IGNORED - pass actor per call)")
    return "  ".join(bits)


if __name__ == "__main__":
    run("state-mcp", "1.0", banner)
