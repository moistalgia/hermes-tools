#!/usr/bin/env python3
"""
paradigm-mcp - the climbing training plan, as data.

Paradigm's calendar is a Next.js app, and the obvious way in is the wrong one.
The page server-renders its whole payload into the HTML as an RSC flight stream,
which is parseable - this server was written that way first, and it worked - but
underneath the app there is a plain JSON API, and it is strictly better:

    GET /api/p-1/calendars/{user}?from=YYYY-MM-DD&to=YYYY-MM-DD
    GET /api/p-1/calendars/{user}/sessions/{ts_id}

Both authenticate with the ordinary NextAuth session cookie. No flight-stream
framing, no double-decoding, no re-reverse-engineering the page every time the
front end is rebuilt. The user id comes from `/api/auth/session`.

The two endpoints are not redundant, and the difference is easy to get wrong:

  * **The calendar endpoint** returns the whole window in one request - 91
    sessions across two months here - but its sessions carry only the *workout*
    sections, and its exercises are `exerciseID` pointers. Names and demo videos
    live in sibling `exercises` and `resources` tables that have to be joined.

  * **The session endpoint** returns one session with every section, including
    the **warm-up**, and with exercise names inline. The warm-up is not in the
    calendar response at all - not in the JSON, not in the HTML. It is fetched
    per session by the client, so it costs one request per session to obtain.

Hence the sync strategy: one cheap request indexes everything, and sessions
inside a near window are then enriched with their detail. Asking for six months
of warm-ups would be 90-odd requests for data that mostly describes a session
you will not do for weeks.

Everything is cached to disk. `sync` refreshes; every read tool serves the cache
and reports its age. A stale answer that says how stale it is beats a failed one.
The payload also carries `logbookEntry` records - what was actually performed,
not merely prescribed - so snapshots accumulate real history.

Two shapes come out, because two questions get asked:

    summary   session names and durations, rolled up per day. What a daily brief
              and a calendar want: "Ideal Circuit 3h 13m, Legs/Core 1h 10m,
              4h 23m total".
    detail    the tree underneath - sections, groups, exercises, sets, reps,
              rest, intensity, notes and demo videos.

Durations are **ranges** in the source (`2h 27m - 3h 13m`). Where a single
number is required - a calendar block - the **maximum** is used. Under-booking
training time is the failure that costs something; finishing early is free.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python paradigm_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python paradigm_mcp_server.py sync
         python paradigm_mcp_server.py day date=2026-08-11
         python paradigm_mcp_server.py day detail=true
         python paradigm_mcp_server.py week
         python paradigm_mcp_server.py calendar_blocks

Environment:
    PARADIGM_USERNAME  The sign-in address. Read from `.env` beside this file if
                       not already in the environment.
    PARADIGM_PASSWORD  Ditto. `.env` is gitignored; it is never logged, never
                       echoed, and never passed on a command line.
    PARADIGM_BASE      Override the host. Default
                       https://training.paradigmclimbing.com
    PARADIGM_CACHE_DIR Where the snapshot and cookie jar live. Default `cache/`
                       beside this file.
    PARADIGM_TIMEOUT   Seconds for one HTTP call. Default 90.
    PARADIGM_DETAIL_DAYS
                       How far ahead to pull per-session detail during sync.
                       Default 21. Zero skips detail entirely.
    PARADIGM_THROTTLE  Minimum seconds between API calls. Default 1.2. The API
                       burst-limits at roughly ten rapid requests, so this is
                       what keeps a long sync from being throttled.

No dependencies. urllib and the standard library only.
"""

import datetime as dt
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, s, run, tool  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = os.environ.get("PARADIGM_BASE", "https://training.paradigmclimbing.com").rstrip("/")
CACHE_DIR = os.environ.get("PARADIGM_CACHE_DIR", os.path.join(HERE, "cache"))
TIMEOUT = int(os.environ.get("PARADIGM_TIMEOUT", "90"))
DETAIL_DAYS = int(os.environ.get("PARADIGM_DETAIL_DAYS", "21"))
THROTTLE = float(os.environ.get("PARADIGM_THROTTLE", "1.2"))

SNAPSHOT = os.path.join(CACHE_DIR, "plan.json")
COOKIE_JAR = os.path.join(CACHE_DIR, "cookies.txt")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Section types, by the ids this account's plan actually uses. Observed, not
# documented: there is no endpoint that names them. Anything unrecognised falls
# back to its position, so a new type degrades to "Section 3" instead of hiding.
SECTION_KINDS = {
    "wt_01hhg52bz9ete801mgbj0eq7c7": "Warm-up",
    "wt_01hhg52tnkefmr64bqhw0ez7at": "Workout",
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_env_file(path=None):
    """Fold `.env` into the environment. Existing values win, and nothing is echoed."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


def credentials():
    load_env_file()
    user = os.environ.get("PARADIGM_USERNAME", "").strip()
    password = os.environ.get("PARADIGM_PASSWORD", "")
    if not user or not password:
        raise ToolError(
            "No credentials. Put PARADIGM_USERNAME and PARADIGM_PASSWORD in "
            f"{os.path.join(HERE, '.env')} (copy .env.example), or set them in "
            "the MCP config for this server."
        )
    return user, password


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def opener():
    os.makedirs(CACHE_DIR, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(COOKIE_JAR)
    if os.path.exists(COOKIE_JAR):
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass  # a corrupt jar is not worth failing over; log in again instead
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA)]
    return op, jar


def http_get(op, path, headers=None):
    req = urllib.request.Request(BASE + path)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with op.open(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8")


def http_post_form(op, path, fields, headers=None):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with op.open(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8")


def auth_session(op):
    """NextAuth answers `{}` for an anonymous caller and a user object otherwise."""
    try:
        _, body = http_get(op, "/api/auth/session")
        return json.loads(body or "{}")
    except Exception:
        return {}


def login(op, jar):
    """NextAuth credentials flow: take a CSRF token, then post it back with the login."""
    user, password = credentials()
    _, body = http_get(op, "/api/auth/csrf")
    csrf = json.loads(body)["csrfToken"]

    http_post_form(
        op,
        "/api/auth/callback/credentials",
        {
            "csrfToken": csrf,
            "username": user,
            "password": password,
            "callbackUrl": f"{BASE}/app/calendar",
            "json": "true",
        },
        {"Origin": BASE, "Referer": f"{BASE}/auth/signin"},
    )

    session = auth_session(op)
    if not session:
        raise ToolError(
            "Sign-in failed. The credentials were rejected, or the login flow "
            "changed. Check PARADIGM_USERNAME / PARADIGM_PASSWORD by signing in "
            "at the website with the same values."
        )
    jar.save(ignore_discard=True, ignore_expires=True)
    return session


def connect():
    """An opener with a live session, plus the user id the API paths need."""
    op, jar = opener()
    session = auth_session(op)
    if not session:
        session = login(op, jar)
    user = (session.get("user") or {}).get("id")
    user_id = user.get("id") if isinstance(user, dict) else user
    if not user_id:
        raise ToolError(f"Signed in, but no user id in the session payload: {session!r}")
    return op, user_id


_last_call = 0.0


def api_get(op, path, retries=4):
    """One JSON call, paced and willing to wait.

    The API burst-limits: a tight loop over per-session detail starts returning
    429 after about ten requests, then serves normally again a moment later. So
    calls are spaced by `PARADIGM_THROTTLE` and a 429 is retried with backoff
    rather than treated as failure. Being slow here is free; being rude is not.
    """
    global _last_call
    delay = 1.0
    for attempt in range(retries + 1):
        gap = THROTTLE - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()
        try:
            _, body = http_get(
                op, path, {"Accept": "application/json", "X-From-Client": "true"}
            )
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait = exc.headers.get("Retry-After")
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = delay
                    delay *= 2
                time.sleep(min(wait, 30))
                continue
            raise ToolError(f"GET {path} returned HTTP {exc.code}.")
    else:
        raise ToolError(f"GET {path} kept returning 429 after {retries} retries.")

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise ToolError(f"GET {path} did not return JSON. First bytes: {body[:120]!r}")


def fetch_calendar(op, user_id, start, end):
    query = urllib.parse.urlencode({"from": start, "to": end})
    return api_get(op, f"/api/p-1/calendars/{user_id}?{query}")


def fetch_session(op, user_id, session_id):
    return api_get(op, f"/api/p-1/calendars/{user_id}/sessions/{session_id}")


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def hms_to_minutes(value):
    """`{"h":1,"m":5,"s":null}` -> 65.0. Kept in minutes, fractions and all.

    Sub-minute values are real here - a 30 second isometric hold is 0.5 - so
    this must not round to whole minutes.
    """
    if not isinstance(value, dict):
        return None
    hours = value.get("h") or 0
    minutes = value.get("m") or 0
    seconds = value.get("s") or 0
    total = hours * 60 + minutes + seconds / 60
    return round(total, 3) or None


def fmt_minutes(minutes):
    """65 -> '1h 5m'; 0.5 -> '30s'. Reads better than a bare number in a brief."""
    if not minutes:
        return None
    if minutes < 1:
        return f"{int(round(minutes * 60))}s"
    total = int(round(minutes))
    hours, rest = divmod(total, 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    if hours:
        return f"{hours}h"
    return f"{rest}m"


def num(value):
    """Drop a pointless `.0`. RPE 7 reads as intensity; RPE 7.0 reads as a bug."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def quill_to_text(value):
    """Flatten Quill delta ops to plain text. Anything else passes through.

    Rich text arrives as `{"ops": [{"insert": "...", "attributes": {...}}]}`,
    sometimes as a JSON string of that. Formatting is dropped deliberately:
    everything downstream is a brief or an agent prompt.
    """
    data = value
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("{") and '"ops"' in stripped:
            try:
                data = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return value
        else:
            return value or None
    if isinstance(data, dict) and isinstance(data.get("ops"), list):
        out = []
        for op in data["ops"]:
            if isinstance(op, dict) and isinstance(op.get("insert"), str):
                out.append(op["insert"])
        return "".join(out).strip() or None
    return None


def unwrap_id(value):
    """Ids arrive as `{"id": "ts_..."}` in some places and bare in others."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def cycle_value(selector, cycle_length):
    """Read a `"6:1,7:1,8:1,9:1"` selector for this athlete's cycle length.

    The selector exists so one plan can serve a 6-to-9-day training week. The
    session already carries a concrete date, so this only ever refines the
    prescription; it never decides which day anything lands on.
    """
    if not isinstance(selector, str) or not selector.strip() or not cycle_length:
        return None
    for pair in selector.split(","):
        key, _, value = pair.partition(":")
        if key.strip().isdigit() and int(key) == cycle_length:
            return value.strip() or None
    return None


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------


def index_by_id(rows):
    out = {}
    for row in rows or []:
        if isinstance(row, dict):
            ident = unwrap_id(row.get("id"))
            if isinstance(ident, str):
                out[ident] = row
    return out


def media_url(entry):
    """Pull the watchable link out of a media item's entries."""
    for item in (entry or {}).get("entries") or []:
        content = item.get("content")
        if isinstance(content, str) and "youtube.com/watch" in content:
            return content
    return None


def pick_instruction(entry, cycle_length):
    """The prescription for this entry, whichever shape the endpoint used.

    The two endpoints disagree, and the disagreement is silent: the calendar
    gives a single `instruction` object, while the session gives an
    `instructions` *list* of variants plus a `defaultInstructionPosition`
    naming the one in force. Read only the singular field against a session
    response and every exercise comes back with no sets, no reps and no
    duration - present, named, and empty.

    `defaultInstructionPosition` is the app's own explicit answer, so it wins.
    A `cycleSelector` that matches this athlete's cycle length is the next best
    signal, and failing both, the first variant.
    """
    if not isinstance(entry, dict):
        return {}
    single = entry.get("instruction")
    if isinstance(single, dict):
        return single

    options = [o for o in entry.get("instructions") or [] if isinstance(o, dict)]
    if not options:
        return {}

    default = entry.get("defaultInstructionPosition")
    if default is not None:
        for option in options:
            if option.get("position") == default:
                return option
    for option in options:
        if cycle_value(option.get("cycleSelector"), cycle_length):
            return option
    return options[0]


def norm_exercise(entry, cycle_length, exercise_lib, media_lib):
    """One prescribed exercise: what to do, how much, how hard.

    The name may be inline (session endpoint) or behind an `exerciseID` pointer
    (calendar endpoint), so both are tried before giving up.
    """
    if not isinstance(entry, dict):
        return None

    instruction = pick_instruction(entry, cycle_length)
    ref = unwrap_id(entry.get("exerciseID"))
    library = exercise_lib.get(ref) or {}
    inline = entry.get("exercise") if isinstance(entry.get("exercise"), dict) else {}
    name = entry.get("name") or inline.get("name") or library.get("name")
    if not name:
        return None

    # The session endpoint embeds an exercise's media rather than pointing into
    # a shared table, so fold anything inline into the lookup for this call.
    if inline.get("media"):
        media_lib = dict(media_lib)
        media_lib.update(index_by_id(inline["media"]))
    if not instruction:
        instruction = pick_instruction(inline, cycle_length)

    def rng(low_key, high_key, convert=lambda x: x):
        low, high = entry.get(low_key), entry.get(high_key)
        if low is None and high is None:
            low, high = instruction.get(low_key), instruction.get(high_key)
        low, high = convert(low), convert(high)
        if low is None and high is None:
            return None
        return {"min": low, "max": high}

    pin = unwrap_id(entry.get("mediaPin") or library.get("mediaPin") or inline.get("mediaPin"))
    out = {
        "name": name,
        "exercise_id": ref,
        "type": instruction.get("exerciseType"),
        "position": instruction.get("position") or entry.get("position"),
        "optional": bool(entry.get("optional")),
        "is_rest": bool(instruction.get("isRest")),
        "per_side": bool(instruction.get("perSide")),
        "sets": rng("minSets", "maxSets"),
        "reps": rng("minReps", "maxReps"),
        "duration_min": rng("minDuration", "maxDuration", hms_to_minutes),
        "set_duration_min": rng("minSetDuration", "maxSetDuration", hms_to_minutes),
        "set_rest_min": rng("minSetRest", "maxSetRest", hms_to_minutes),
        "rest_min": rng("minRest", "maxRest", hms_to_minutes),
        "rep_rest_min": hms_to_minutes(instruction.get("repRest")),
        "rep_duration_min": hms_to_minutes(instruction.get("repDuration")),
        "note": quill_to_text(entry.get("note")) or quill_to_text(library.get("note")),
        "description": quill_to_text(entry.get("shortDescription"))
        or quill_to_text(library.get("shortDescription")),
        "cycle_note": cycle_value(instruction.get("cycleSelector"), cycle_length),
        "video": media_url(media_lib.get(pin)) if pin else None,
    }

    if instruction.get("intensityType"):
        out["intensity"] = {
            "type": instruction.get("intensityType"),
            "min": instruction.get("minIntensityValue"),
            "max": instruction.get("maxIntensityValue"),
            "reference": instruction.get("intensityReference"),
        }

    logbook = entry.get("logbookEntry")
    if logbook:
        out["logged"] = logbook

    out["prescription"] = describe_prescription(out)
    return {k: v for k, v in out.items() if v not in (None, False, {}, [])}


def describe_prescription(ex):
    """One line a human can read: '1 - 2 sets x 12 reps @ RPE 6'.

    The site renders this from the same fields; reproducing it here means the
    daily brief does not have to reassemble six nullable numbers itself.
    """
    parts = []
    sets = ex.get("sets") or {}
    low, high = num(sets.get("min")), num(sets.get("max"))
    if low and high and low != high:
        parts.append(f"{low} - {high} sets")
    elif high or low:
        parts.append(f"{high or low} sets")

    reps = ex.get("reps") or {}
    rep_low, rep_high = num(reps.get("min")), num(reps.get("max"))
    set_duration = (ex.get("set_duration_min") or {}).get("max")
    if rep_low and rep_high and rep_low != rep_high:
        parts.append(f"{rep_low} - {rep_high} reps")
    elif rep_high or rep_low:
        parts.append(f"{rep_high or rep_low} reps")
    elif set_duration:
        parts.append(fmt_minutes(set_duration))

    line = " x ".join(parts)
    duration = (ex.get("duration_min") or {}).get("max")
    if not line and duration:
        line = fmt_minutes(duration)

    intensity = ex.get("intensity") or {}
    if intensity:
        reference = (intensity.get("reference") or "").upper()
        value_low, value_high = num(intensity.get("min")), num(intensity.get("max"))
        if value_low and value_high and value_low != value_high:
            value = f"{value_low}-{value_high}"
        else:
            value = str(value_high or value_low or "").strip()
        if value:
            unit = "%" if intensity.get("type") == "percentage" else ""
            line = f"{line} @ {value}{unit} {reference}".strip()

    if ex.get("per_side"):
        line += " per side"
    return line or None


def norm_group(group, cycle_length, exercise_lib, media_lib):
    """One block within a section: a circuit, a superset, a single movement.

    Groups are where the A/A/B/B pairing on the calendar comes from and where
    "repeat this three times" is stated, so they are kept rather than flattened.
    """
    instruction = pick_instruction(group, cycle_length)
    exercises = []
    for entry in group.get("exercises") or []:
        normalised = norm_exercise(entry, cycle_length, exercise_lib, media_lib)
        if normalised:
            exercises.append(normalised)

    low, high = (
        hms_to_minutes(instruction.get("minDuration")),
        hms_to_minutes(instruction.get("maxDuration")),
    )
    repeats_low, repeats_high = instruction.get("minRepeats"), instruction.get("maxRepeats")
    rest_low, rest_high = (
        hms_to_minutes(instruction.get("minRest")),
        hms_to_minutes(instruction.get("maxRest")),
    )

    out = {
        "position": group.get("position") or instruction.get("position"),
        "number": group.get("runningNumber"),
        "label": group.get("groupIdentifier"),
        "optional": bool(group.get("optional")),
        "repeats": {"min": repeats_low, "max": repeats_high} if (repeats_low or repeats_high) else None,
        "duration_min": {"min": low, "max": high} if (low or high) else None,
        "rest_min": {"min": rest_low, "max": rest_high} if (rest_low or rest_high) else None,
        "label": group.get("groupIdentifier"),
        "pick": group.get("groupSelector"),
        "note": quill_to_text(group.get("note")),
        "exercises": exercises,
    }
    return {k: v for k, v in out.items() if v not in (None, False, {}, [])}


def section_duration(groups):
    """Total a section, respecting alternatives.

    Groups sharing a `label` are choices, not a queue - the app renders them as
    "Do 1 from Group A" - and `pick` says how many. Summing them all inflates a
    three-hour session into a four-hour one, which is exactly the sort of error
    that looks plausible on a calendar and is wrong every single day.

    So: ungrouped work counts in full, and each labelled set contributes only
    its `pick` cheapest options to the minimum and `pick` dearest to the maximum.
    """
    low = high = 0.0
    alternatives = {}
    for group in groups:
        window = group.get("duration_min") or {}
        label = group.get("label")
        if label:
            alternatives.setdefault(label, []).append(group)
        else:
            low += window.get("min") or window.get("max") or 0
            high += window.get("max") or window.get("min") or 0

    for label, members in alternatives.items():
        pick = members[0].get("pick") or 1
        lows = sorted((g.get("duration_min") or {}).get("min")
                      or (g.get("duration_min") or {}).get("max") or 0 for g in members)
        highs = sorted((g.get("duration_min") or {}).get("max")
                       or (g.get("duration_min") or {}).get("min") or 0 for g in members)
        low += sum(lows[:pick])
        high += sum(highs[-pick:])

    return (round(low, 3) or None, round(high, 3) or None)


def norm_section(section, position, cycle_length, exercise_lib, media_lib):
    kind = SECTION_KINDS.get(section.get("workoutType")) or f"Section {position}"
    groups = []
    for group in section.get("exerciseGroups") or []:
        if isinstance(group, dict):
            groups.append(norm_group(group, cycle_length, exercise_lib, media_lib))
    groups.sort(key=lambda g: (g.get("position") or 0, g.get("number") or 0))

    low, high = section_duration(groups)
    choices = sorted({g["label"] for g in groups if g.get("label")})
    return {
        "kind": kind,
        "type_id": section.get("workoutType"),
        "duration_min": {"min": low, "max": high} if (low or high) else None,
        "duration_label": fmt_minutes(high),
        "choose_from": choices or None,
        "groups": groups,
    }


def norm_session(raw, exercise_lib, media_lib, detailed=False):
    """One training block - 'Ideal Circuit', 'Legs/Core Supplementals'."""
    cycle_length = raw.get("cycleLength")
    sections = []
    for position, section in enumerate(raw.get("sections") or [], start=1):
        if isinstance(section, dict):
            sections.append(
                norm_section(section, position, cycle_length, exercise_lib, media_lib)
            )

    exercises = [e for sec in sections for g in sec["groups"] for e in g.get("exercises", [])]
    low, high = hms_to_minutes(raw.get("minDuration")), hms_to_minutes(raw.get("maxDuration"))

    return {
        "id": unwrap_id(raw.get("id")),
        "name": raw.get("name"),
        "date": raw.get("date") or raw.get("onDate"),
        "position": raw.get("position"),
        "optional": bool(raw.get("optional")),
        "cycle": raw.get("cycle"),
        "cycle_length": cycle_length,
        "note": quill_to_text(raw.get("note")),
        "description": quill_to_text(raw.get("description")),
        "duration_min": {"min": low, "max": high},
        "duration_label": fmt_minutes(high),
        "detailed": detailed,
        # Sessions are scheduled months ahead but only prescribed a few weeks
        # out: past a point they carry a name, a date and a duration with no
        # exercises at all. That is the plan working as intended, not a parse
        # failure, and the difference has to survive into the output or a brief
        # will report an empty session as though the content had gone missing.
        "prescribed": bool(exercises),
        "section_summary": [
            f"{sec['kind']}{' ' + sec['duration_label'] if sec['duration_label'] else ''}"
            for sec in sections
        ],
        "exercise_count": len(exercises),
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def save_plan(plan):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1, ensure_ascii=False, default=str)


def load_plan():
    if not os.path.exists(SNAPSHOT):
        raise ToolError("No cached plan yet. Run `sync` first.")
    with open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


def cache_age_hours(plan):
    try:
        then = dt.datetime.fromisoformat(plan["fetched_at"])
        return round((dt.datetime.now().astimezone() - then).total_seconds() / 3600, 1)
    except Exception:
        return None


def today_iso():
    return dt.date.today().isoformat()


def parse_date(value, fallback=None):
    value = (value or "").strip()
    if not value:
        return fallback or today_iso()
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ToolError(f"Bad date {value!r}. Use YYYY-MM-DD.")


def summarise(session):
    """The high-level view: what it is, how long to book, what it is made of."""
    return {
        "id": session.get("id"),
        "name": session.get("name"),
        "duration_label": session.get("duration_label"),
        "minutes": (session.get("duration_min") or {}).get("max"),
        "optional": session.get("optional"),
        "sections": session.get("section_summary"),
        "exercise_count": session.get("exercise_count"),
        "prescribed": session.get("prescribed"),
        "detailed": session.get("detailed"),
        "note": session.get("note"),
    }


def day_payload(plan, date, detail=False):
    sessions = [x for x in plan["sessions"] if x["date"] == date]
    sessions.sort(key=lambda x: x.get("position") or 0)
    total = sum((x.get("duration_min") or {}).get("max") or 0 for x in sessions)
    return {
        "date": date,
        "rest_day": not sessions,
        "session_count": len(sessions),
        "total_minutes": total or None,
        "total_label": fmt_minutes(total),
        "sessions": sessions if detail else [summarise(x) for x in sessions],
    }


def day_line(payload):
    if payload["rest_day"]:
        return f"{payload['date']}: rest day, nothing scheduled."
    names = ", ".join(
        f"{x['name']} ({x['duration_label']})"
        for x in (payload["sessions"] if not payload["sessions"] or "minutes" in payload["sessions"][0]
                  else [summarise(x) for x in payload["sessions"]])
    )
    return f"{payload['date']}: {names}. Total {payload['total_label']}."


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "Log in, fetch the plan, and refresh the cache. Every other tool serves the "
    "cache, so run this first and whenever the plan may have changed.",
    {
        "start": s("YYYY-MM-DD to fetch from. Defaults to 30 days ago."),
        "end": s("YYYY-MM-DD to fetch to. Defaults to 180 days ahead."),
        "detail_days": i(
            "How far ahead to pull per-session detail, which is the only source "
            "of warm-ups. One request per session, so keep it modest.",
            default=DETAIL_DAYS,
            minimum=0,
        ),
    },
)
def sync(start=None, end=None, detail_days=None):
    op, user_id = connect()
    detail_days = DETAIL_DAYS if detail_days is None else max(0, int(detail_days))

    today = dt.date.today()
    first = parse_date(start, (today - dt.timedelta(days=30)).isoformat())
    last = parse_date(end, (today + dt.timedelta(days=180)).isoformat())

    raw = fetch_calendar(op, user_id, first, last)
    exercise_lib = index_by_id(raw.get("exercises"))
    media_lib = index_by_id(raw.get("resources"))
    raw_sessions = raw.get("sessions") or []
    if not raw_sessions:
        raise ToolError(
            f"The calendar returned no sessions between {first} and {last}. "
            "Widen the range, or check the plan is still assigned."
        )

    # Detail is per-session and only reaches so far forward; the rest stay at
    # calendar fidelity, which is complete except for warm-ups.
    horizon = today + dt.timedelta(days=detail_days)
    sessions, detailed, failed = [], 0, []
    for entry in raw_sessions:
        date = entry.get("date") or entry.get("onDate")
        session_id = unwrap_id(entry.get("id"))
        want_detail = (
            detail_days
            and session_id
            and date
            and today <= dt.date.fromisoformat(date) <= horizon
        )
        if want_detail:
            try:
                entry = fetch_session(op, user_id, session_id)
                detailed += 1
            except ToolError as exc:
                failed.append(f"{session_id}: {exc}")
                want_detail = False
        sessions.append(norm_session(entry, exercise_lib, media_lib, bool(want_detail)))

    sessions = [x for x in sessions if x.get("date")]
    sessions.sort(key=lambda x: (x["date"], x.get("position") or 0))
    dates = [x["date"] for x in sessions]

    plan = {
        "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "requested_range": {"start": first, "end": last},
        "date_range": {"start": min(dates), "end": max(dates)},
        "session_count": len(sessions),
        "detailed_count": detailed,
        "sessions": sessions,
    }
    save_plan(plan)

    result = {
        "ok": True,
        "summary": (
            f"Synced {len(sessions)} sessions ({min(dates)} to {max(dates)}), "
            f"{detailed} with full detail."
        ),
        "session_count": len(sessions),
        "detailed_count": detailed,
        "date_range": plan["date_range"],
        "cache": SNAPSHOT,
    }
    if failed:
        result["detail_failures"] = failed[:10]
    return result


@tool(
    "The training for one day: what to do and how long to book. Defaults to "
    "today. Pass detail=true for sections, exercises, sets and reps.",
    {
        "date": s("YYYY-MM-DD. Defaults to today."),
        "detail": b("Include the full breakdown.", False),
    },
)
def day(date=None, detail=False):
    plan = load_plan()
    when = parse_date(date)
    payload = day_payload(plan, when, detail)
    payload["cache_age_hours"] = cache_age_hours(plan)
    if payload["rest_day"]:
        payload["summary"] = f"{when}: rest day, nothing scheduled."
    else:
        names = ", ".join(
            f"{x.get('name')} ({x.get('duration_label')})" for x in payload["sessions"]
        )
        payload["summary"] = f"{when}: {names}. Total {payload['total_label']}."
    return payload


@tool(
    "A week of training, one entry per day, with the weekly total. Defaults to "
    "the week containing today (Monday start).",
    {
        "start": s("YYYY-MM-DD for the first day. Defaults to this week's Monday."),
        "days": i("How many days.", default=7, minimum=1, maximum=90),
        "detail": b("Include the full breakdown for every day.", False),
    },
)
def week(start=None, days=7, detail=False):
    plan = load_plan()
    if start:
        first = dt.date.fromisoformat(parse_date(start))
    else:
        today = dt.date.today()
        first = today - dt.timedelta(days=today.weekday())

    span = max(1, min(int(days), 90))
    out = [
        day_payload(plan, (first + dt.timedelta(days=n)).isoformat(), detail)
        for n in range(span)
    ]
    total = sum(d.get("total_minutes") or 0 for d in out)
    training_days = sum(1 for d in out if not d["rest_day"])
    return {
        "ok": True,
        "start": first.isoformat(),
        "days": span,
        "training_days": training_days,
        "total_minutes": total or None,
        "total_label": fmt_minutes(total),
        "cache_age_hours": cache_age_hours(plan),
        "summary": (
            f"{first.isoformat()} +{span}d: {training_days} training days, "
            f"{fmt_minutes(total)} total."
        ),
        "days_detail": out,
    }


@tool(
    "One session in full - every section, exercise, set, rep, rest and "
    "intensity, with demo video links.",
    {"id": s("The session id, e.g. ts_01ky7vs3d3e1bt43tskjc561k2.")},
    required=["id"],
)
def session(id):
    plan = load_plan()
    for entry in plan["sessions"]:
        if entry.get("id") == id:
            result = {"ok": True, "cache_age_hours": cache_age_hours(plan), "session": entry}
            if not entry.get("detailed"):
                result["note"] = (
                    "Calendar-fidelity only: the warm-up is missing. Re-run sync "
                    "with a detail_days window that covers this date."
                )
            return result
    raise ToolError(
        f"No session {id!r} in the cache.",
        examples=[e["id"] for e in plan["sessions"][:5] if e.get("id")],
    )


@tool(
    "Calendar-ready blocks: one entry per session, sized to the maximum "
    "duration. This is the feed for the Google Calendar sync.",
    {
        "start": s("YYYY-MM-DD. Defaults to today."),
        "end": s("YYYY-MM-DD. Defaults to 28 days after start."),
    },
)
def calendar_blocks(start=None, end=None):
    plan = load_plan()
    first = dt.date.fromisoformat(parse_date(start))
    last = dt.date.fromisoformat(
        parse_date(end, (first + dt.timedelta(days=28)).isoformat())
    )
    if last < first:
        raise ToolError(f"end ({last}) is before start ({first}).")

    blocks = []
    for entry in plan["sessions"]:
        when = dt.date.fromisoformat(entry["date"])
        if not (first <= when <= last):
            continue
        minutes = (entry.get("duration_min") or {}).get("max")
        blocks.append({
            "date": entry["date"],
            "title": entry.get("name"),
            "minutes": minutes,
            "duration_label": fmt_minutes(minutes),
            "optional": entry.get("optional"),
            "prescribed": entry.get("prescribed"),
            "session_id": entry.get("id"),
            "sections": entry.get("section_summary"),
            "note": entry.get("note"),
            "exercises": [
                e.get("name")
                for sec in entry.get("sections", [])
                for g in sec.get("groups", [])
                for e in g.get("exercises", [])
            ][:15],
        })
    total = sum(x["minutes"] or 0 for x in blocks)
    return {
        "ok": True,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "count": len(blocks),
        "total_label": fmt_minutes(total),
        "sizing": "max",
        "cache_age_hours": cache_age_hours(plan),
        "summary": (
            f"{len(blocks)} sessions between {first} and {last}, "
            f"{fmt_minutes(total)} total, each sized to its maximum duration."
        ),
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Calendar export
# ---------------------------------------------------------------------------

# Matches the Google calendar these events are imported into. Only a display
# hint - on import Google uses the destination picked in its own dialog, and
# this name is read solely when a client subscribes to the file by URL.
CALENDAR_NAME = os.environ.get("PARADIGM_CALENDAR_NAME", "Climbing")


def event_description(sessions):
    """The body of a day's event: what the training actually is.

    All-day events give a one-line title and a body nobody sees until they open
    it, so the title carries the decision - how many hours - and this carries
    the content.
    """
    lines = []
    for session in sessions:
        header = f"{session.get('name')} - {session.get('duration_label') or 'no duration'}"
        if session.get("optional"):
            header += " (optional)"
        lines.append(header)

        if not session.get("prescribed"):
            lines.append("  Not published yet - scheduled, but no exercises written.")
            lines.append("")
            continue

        if session.get("note"):
            lines.append(f"  Coach: {session['note']}")

        for section in session.get("sections") or []:
            label = section.get("kind")
            if section.get("duration_label"):
                label += f" ({section['duration_label']})"
            if section.get("choose_from"):
                label += f" - choose from group {', '.join(section['choose_from'])}"
            lines.append(f"  {label}")
            for group in section.get("groups") or []:
                marker = f"[{group['label']}] " if group.get("label") else ""
                for exercise in group.get("exercises") or []:
                    detail = exercise.get("prescription") or ""
                    lines.append(
                        f"    {marker}{exercise['name']}" + (f" - {detail}" if detail else "")
                    )
        lines.append("")

    if not any(s.get("detailed") for s in sessions):
        lines.append("(Warm-up detail not synced for this day.)")
    return "\n".join(lines).strip()


def day_event(plan, date, include_rest=False):
    """One all-day event for one training day, or None for a rest day."""
    payload = day_payload(plan, date, detail=True)
    sessions = payload["sessions"]
    if not sessions:
        if not include_rest:
            return None
        return {
            "date": date,
            "title": "Rest day",
            "minutes": 0,
            "duration_label": None,
            "prescribed": True,
            "sessions": [],
            "description": "No training scheduled.",
        }

    names = ", ".join(s.get("name") or "Training" for s in sessions)
    total = payload["total_label"]
    unwritten = [s for s in sessions if not s.get("prescribed")]
    title = f"{total} - {names}" if total else names
    if unwritten:
        title += " (not published yet)"

    return {
        "date": date,
        "title": title,
        "minutes": payload["total_minutes"],
        "duration_label": total,
        "prescribed": not unwritten,
        "sessions": [s.get("name") for s in sessions],
        "session_ids": [s.get("id") for s in sessions],
        "description": event_description(sessions),
    }


def ics_escape(text):
    """RFC 5545 escaping. Order matters - backslash first, or it doubles itself."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def ics_fold(line):
    """Fold to 75 octets per RFC 5545. Long descriptions are otherwise dropped.

    The limit counts bytes, not characters, and a continuation line spends one
    of its 75 on the leading space. Multi-byte characters must never be split
    across the boundary, so a chunk is shortened until it decodes cleanly.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    parts, first = [], True
    while raw:
        chunk = raw[: 75 if first else 74]
        while chunk:
            try:
                text = chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        else:
            break
        parts.append(text)
        raw = raw[len(chunk):]
        first = False
    return "\r\n ".join(parts)


def build_ics(events, name=CALENDAR_NAME):
    """A subscribable/importable calendar.

    UIDs are derived from the date, so re-importing updates the existing event
    rather than stacking a second copy of every training day on top of the first.
    """
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hermes-tools//paradigm-mcp//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:UTC",
    ]
    for event in events:
        compact = event["date"].replace("-", "")
        end = (dt.date.fromisoformat(event["date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:paradigm-{compact}@hermes-tools",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{compact}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{ics_escape(event['title'])}",
            f"DESCRIPTION:{ics_escape(event['description'])}",
            "TRANSP:TRANSPARENT",  # all-day training must not mark the day busy
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


@tool(
    "One all-day entry per training day - the hours total plus what it is. This "
    "is the shape the calendar and the daily brief both want.",
    {
        "start": s("YYYY-MM-DD. Defaults to today."),
        "end": s("YYYY-MM-DD. Defaults to 28 days after start."),
        "include_rest": b("Emit rest days too, instead of leaving them empty.", False),
    },
)
def calendar_days(start=None, end=None, include_rest=False):
    plan = load_plan()
    first = dt.date.fromisoformat(parse_date(start))
    last = dt.date.fromisoformat(
        parse_date(end, (first + dt.timedelta(days=28)).isoformat())
    )
    if last < first:
        raise ToolError(f"end ({last}) is before start ({first}).")

    events, day_count = [], (last - first).days + 1
    for offset in range(day_count):
        event = day_event(plan, (first + dt.timedelta(days=offset)).isoformat(), include_rest)
        if event:
            events.append(event)

    total = sum(e["minutes"] or 0 for e in events)
    unwritten = sum(1 for e in events if not e["prescribed"])
    result = {
        "ok": True,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "count": len(events),
        "total_label": fmt_minutes(total),
        "sizing": "max",
        "cache_age_hours": cache_age_hours(plan),
        "summary": (
            f"{len(events)} training days between {first} and {last}, "
            f"{fmt_minutes(total)} total."
        ),
        "events": events,
    }
    if unwritten:
        result["not_yet_published"] = unwritten
    return result


@tool(
    "Write the training days to an .ics file: all-day events, one per training "
    "day. Import it into a new Google Calendar, or host it and subscribe.",
    {
        "path": s("Where to write. Defaults to `training.ics` in the cache directory."),
        "start": s("YYYY-MM-DD. Defaults to today."),
        "end": s("YYYY-MM-DD. Defaults to the end of the cached plan."),
        "include_rest": b("Emit rest days too.", False),
    },
)
def export_ics(path=None, start=None, end=None, include_rest=False):
    plan = load_plan()
    first = parse_date(start)
    last = parse_date(end, plan["date_range"]["end"])
    feed = calendar_days(start=first, end=last, include_rest=include_rest)
    if not feed["events"]:
        raise ToolError(f"No training days between {first} and {last}, so nothing to write.")

    target = path or os.path.join(CACHE_DIR, "training.ics")
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    # Written as bytes with CRLF already in the text, so newline handling on
    # Windows cannot turn every line ending into CRCRLF.
    with open(target, "wb") as fh:
        fh.write(build_ics(feed["events"]).encode("utf-8"))

    return {
        "ok": True,
        "path": os.path.abspath(target),
        "events": feed["count"],
        "start": first,
        "end": last,
        "total_label": feed["total_label"],
        "calendar_name": CALENDAR_NAME,
        "summary": (
            f"Wrote {feed['count']} all-day events ({first} to {last}, "
            f"{feed['total_label']} of training) to {os.path.abspath(target)}."
        ),
        "next_step": (
            f"In Google Calendar: Settings > Import & export > Import, choose "
            f"this file, and set the destination calendar to {CALENDAR_NAME!r}. "
            "Re-importing an updated file updates each day rather than "
            "duplicating it."
        ),
    }


@tool(
    "Sync from Paradigm and rewrite the .ics in one step. This is the one to "
    "put on a schedule; re-import the file when you want the calendar refreshed.",
    {
        "path": s("Where to write the .ics. Defaults to `training.ics` in the cache directory."),
        "detail_days": i(
            "How far ahead to pull per-session detail.", default=DETAIL_DAYS, minimum=0
        ),
        "include_rest": b("Emit rest days too, instead of leaving them empty.", False),
    },
)
def refresh(path=None, detail_days=None, include_rest=False):
    synced = sync(detail_days=detail_days)
    written = export_ics(path=path, include_rest=include_rest)
    # Reported together because "synced fine, wrote nothing" and "wrote a stale
    # file" are both silent failures when these run unattended.
    return {
        "ok": True,
        "summary": f"{synced['summary']} {written['summary']}",
        "session_count": synced["session_count"],
        "detailed_count": synced["detailed_count"],
        "date_range": synced["date_range"],
        "events": written["events"],
        "path": written["path"],
        "total_label": written["total_label"],
        "next_step": written["next_step"],
    }


def banner():
    load_env_file()
    have = "set" if os.environ.get("PARADIGM_PASSWORD") else "MISSING"
    if os.path.exists(SNAPSHOT):
        try:
            plan = load_plan()
            cached = (
                f"{plan['session_count']} sessions "
                f"({plan['detailed_count']} detailed), "
                f"{plan['date_range']['start']}..{plan['date_range']['end']}, "
                f"{cache_age_hours(plan)}h old"
            )
        except Exception:
            cached = "unreadable - re-run sync"
    else:
        cached = "empty - run sync"
    return f"Host: {BASE}\nCredentials: {have}\nCache: {cached}\n  {SNAPSHOT}"


if __name__ == "__main__":
    run("paradigm-mcp", "1.0", banner)
