# paradigm-mcp

The Paradigm Climbing training plan, as data.

No dependencies — `urllib` and the standard library, against
[../mcpkit.py](../mcpkit.py).

## The obvious way in is the wrong one

Paradigm's calendar is a Next.js App Router page. There is no "download my
plan" button, and the reflex is to drive a browser, expand each day, and read
the rendered text back out of the DOM.

Don't. The page server-renders its entire payload into the HTML as a React
Server Components flight stream, so the numbers are already structured — the
work is decoding, not scraping. This server was originally written that way and
it worked, but underneath the app there is a plain JSON API, and it is strictly
better:

```
GET /api/p-1/calendars/{user}?from=YYYY-MM-DD&to=YYYY-MM-DD
GET /api/p-1/calendars/{user}/sessions/{ts_id}
```

Both authenticate with the ordinary NextAuth session cookie. The user id comes
from `/api/auth/session`. No flight-stream framing, no double-decoded JSON, and
nothing that has to be re-derived every time the front end is rebuilt.

*(If that API ever disappears, the flight-stream parser is recoverable from this
file's git history. It is not carried here as dead weight.)*

## The two endpoints are not redundant

This is the part that is easy to get wrong, and getting it wrong looks like
success:

| | calendar endpoint | session endpoint |
| --- | --- | --- |
| Cost | one request for the whole window | one request **per session** |
| Sessions | all of them | one |
| Sections | **workout only** | **all, including the warm-up** |
| Exercise names | `exerciseID` pointers, must be joined | inline, under `exercise` |
| Prescriptions | `instruction`, an object | `instructions`, a **list** |

**The warm-up is not in the calendar response at all** — not in the JSON, not in
the HTML. The client fetches it per session when you open one. A parse that
reads only the calendar therefore produces a session whose totals are right and
whose contents are quietly incomplete.

The last row is the nastier trap. The session endpoint returns a *list* of
instruction variants with a `defaultInstructionPosition` naming the one in
force. Read the singular field against a session response and every exercise
comes back present, correctly named, and with no sets, no reps and no duration —
which looks far more like a plan full of untimed drills than like a bug.

Hence the sync strategy: one cheap request indexes everything, then sessions
inside a near window are enriched with their detail. Pulling six months of
warm-ups would be hundreds of requests for sessions you will not do for weeks.

The API also **burst-limits** — roughly ten rapid requests, then `429`, then
normal service a moment later. Calls are spaced by `PARADIGM_THROTTLE` and a
`429` is retried with backoff. A slow sync is free; a rude one is not.

## Install

Put the credentials in `.env` beside the server (it is gitignored):

```bash
cp .env.example .env
```

Then prove it from a shell before wiring it to anything:

```bash
python paradigm_mcp_server.py sync
```

```bash
python paradigm_mcp_server.py day
```

Register it in an MCP config as `paradigm`:

```json
{
  "mcpServers": {
    "paradigm": {
      "command": "python",
      "args": ["C:/Dev/hermes-tools/paradigm-mcp/paradigm_mcp_server.py", "serve"]
    }
  }
}
```

Credentials can live in the MCP config's `env` block instead of `.env` if you
prefer. They are never logged, never echoed, and never passed on a command line.

## Tools

| Tool | What it answers |
| --- | --- |
| `sync` | Refresh the cache. Everything else reads the cache. |
| `day` | What am I doing today? `detail=true` for sets and reps. |
| `week` | What does this week look like, and how many hours is it? |
| `session` | One session in full, with demo video links. |
| `calendar_days` | One all-day entry per training day — the calendar feed. |
| `calendar_blocks` | The same, split per session rather than per day. |
| `export_ics` | Write those days to an `.ics` file. |
| `refresh` | `sync` + `export_ics` in one call — the one to schedule. |

Everything is cached to disk, and every read reports `cache_age_hours`. A stale
answer that says how stale it is beats a failed one.

## Two shapes, because two questions get asked

```
$ python paradigm_mcp_server.py day date=2026-08-11
2026-08-11: Ideal Circuit (3h 13m), Legs/Core Supplementals (1h 10m).
Total 4h 23m.
```

That is the daily-brief and calendar view. Underneath it, `detail=true` opens
the whole tree — sections, groups, exercises, sets, reps, rest, intensity,
notes, and a YouTube demo per movement:

```
Legs/Core Supplementals — 1h 10m
  Warm-up 15m
    Step 1: Dynamic Stretching                      15m
  Workout 50m - 55m
    Single Leg Romanian Deadlift                    1 - 2 sets x 12 reps @ 5-6 RPE
    Heel-Hook Isometric Pull 60, 90, 120 Degrees    1 - 3 sets x 30s @ 7-8 RPE
    TRX/Rings Body Saws                             1 - 2 sets x 12 reps @ 6 RPE
    Bulgarian Split Squats                          1 - 2 sets x 12 reps @ 6 RPE
```

## Groups sharing a label are alternatives

The app renders these as **"Do 1 from Group A"**. Three groups carry
`groupIdentifier: "A"` and `groupSelector: 1`, and you do *one* of them:

```
Workout 1h 17m - 2h 3m          choose from: A
  Capacity Phase Warning!                    2m - 3m
  [A] Dynos                                 15m - 30m
  [A] Hover Drill                           15m - 30m   @ 7 RPE
  [A] Slab Up-Down Repeats                  20m - 30m
      Ideal Circuit                          1h - 1h 30m
```

Summing all of them turns a three-hour session into a four-hour one — an error
that looks entirely plausible on a calendar and is wrong every single day. So
each labelled set contributes only its `pick` cheapest options to the minimum
and `pick` dearest to the maximum.

That this is right is checkable rather than assumed: the per-group sum
reconciles to the minute with the session's own `minDuration`/`maxDuration`,
which is computed by a completely different path.

## Scheduled is not the same as prescribed

The plan is laid out months further ahead than it is written. Of 282 sessions
here, all 282 have a name, a date and a duration; only about 68 — roughly two
months out — have exercises. The rest are real, dated, and not yet filled in.

This matters in both directions, so it is surfaced as `prescribed`:

- **Calendar work can use everything.** A name, a date and a duration is exactly
  what an event needs, so the whole horizon is bookable today.
- **A brief must not treat an unwritten session as an empty one.** "No exercises"
  and "not published yet" look identical in the data and mean opposite things.

## The calendar is all-day, one event per day

Nothing in the payload says *when* to train — only how long. Inventing a start
time would be a guess that goes stale the first week, so events are **all-day**,
and the useful fact goes in the title where it can be read without opening
anything:

```
Mon 10 Aug   35m - Recovery + Optional Stretching
Tue 11 Aug   4h 23m - Ideal Circuit, Legs/Core Supplementals
Wed 12 Aug   1h 5m - Capacity Phase Mobility Development Day
Thu 13 Aug   4h 31m - Project Practice Or High End Skill Development, Pull Supplementals
```

One event per *day*, not per session — "four hours of training today" is a
day-level fact, and two all-day events for one day is noise. The body of the
event carries the whole breakdown: coach notes, sections, every exercise with
its prescription. Rest days get no event, unless `include_rest` is set.

Events are marked `TRANSP:TRANSPARENT`, so a training day does not show as busy
and block meeting invitations.

`export_ics` writes them out. Event UIDs are derived from the date, so
re-importing an updated file **updates** each day rather than stacking a second
copy on top of the first. That is what makes periodic re-import viable rather
than a slowly accumulating mess.

### Keeping it fresh

The plan is published progressively, so the cache goes stale. The primary way to
keep the Google Calendar in sync is to ask the agent:

> "I needed an extra rest day, refresh my training calendar."

The agent runs `paradigm.refresh include_rest=true` (which re-pulls from
Paradigm and writes a fresh `training.ics`), then pushes that file to Google
Calendar via Hermes' native import tool. Google reconciles by event UID —
existing training-day events are updated in place, new dates are created, and a
day that shifted from training to rest gets a "Rest day" event that overwrites
the stale block. No manual steps. See
[skills/training-calendar-sync/SKILL.md](../skills/training-calendar-sync/SKILL.md)
for the full flow.

**Without Hermes.** If you are not running through Hermes, put `refresh` on a
schedule and import the `.ics` file manually when you want the calendar caught
up:

```bash
python paradigm_mcp_server.py refresh include_rest=true
```

Then import `training.ics` via Google Calendar → Settings → Import & export.
Pass `include_rest=true` so a day that shifted from training to rest gets an
explicit "Rest day" event that overwrites the stale one.

Daily is plenty; the plan does not change hour to hour. Subscribing Google to a
hosted `.ics` sounds better but Google refreshes subscribed calendars on its own
schedule, often a day or more late and with no way to force it.

## Durations are ranges

The source gives `2h 27m - 3h 13m`, not a number. Where a single value is
required — a calendar block — the **maximum** is used. Under-booking training
time is the failure that costs something; finishing early is free.

The website rounds these to the nearest five minutes for display (`2h 30m -
3h 15m`). This server keeps the underlying values.

## Notes on the data

- **`cycleSelector`** (`"6:1,7:1,8:1,9:1"`) lets one plan serve a 6-to-9-day
  training week. Sessions carry a concrete `date`, so the server has already
  resolved the schedule; the selector only ever refines a prescription and never
  decides which day anything lands on.
- **`logbookEntry`** records what was actually performed, not merely prescribed.
  Kept as `logged`, so snapshots accumulate real history.
- **Section types** (`wt_*`) have no lookup endpoint. The two this plan uses are
  mapped by id; anything unrecognised degrades to `Section 3` rather than
  vanishing.
- **Rich text** arrives as Quill delta ops and is flattened to plain text.
  Everything downstream is a brief or an agent prompt.
