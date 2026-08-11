---
name: training-calendar-sync
description: Sync the Paradigm climbing training plan into Google Calendar. Use whenever the user says they've moved their schedule, taken an extra rest day, shifted training forward, or wants their calendar brought up to date after a change in the Paradigm app.
tags: []
related_skills: []
---

# Training Calendar Sync

The Paradigm app is the source of truth for the training plan. This repo never
writes back to it. When the user moves their plan inside Paradigm — taking a
rest day and shifting everything forward, for example — the calendar needs to
catch up. This skill does that in one agent turn.

Everything goes through the **`paradigm` MCP server** and Hermes' native
calendar tools. Those are the only two supported paths. If a calendar tool call
fails, the fix is never a different transport — surface the error and stop. Do
not fall back to writing or importing the `.ics` file silently; that path exists
as a documented manual fallback and is not a silent rescue for a failed tool
call.

## Trigger

The user says something like:

- "I needed an extra rest day, refresh my training calendar"
- "I moved my training up a day, update the calendar"
- "I shifted my plan forward in Paradigm, can you fix the calendar"

The invariant is: the user has already made the change inside the Paradigm app,
and the calendar now needs to reflect that. Do not shift anything in the cache
yourself. Paradigm is the origin; always pull from it first.

## Target calendar

The calendar to sync into is named by the `PARADIGM_CALENDAR_NAME` environment
variable on the `paradigm` server. Its default is `Climbing`. Confirm the
actual value before writing — use the calendar name from the server, not a
hardcoded string.

## Flow

### 1. Refresh from Paradigm

Run `paradigm.refresh`. This syncs the cache from Paradigm and rewrites the
`.ics` file in one step. The `.ics` file is a side effect that costs nothing and
preserves the manual-import fallback; ignore it for this flow.

Do not use `paradigm.sync` here. `refresh` is the correct call: it re-pulls
from Paradigm (so the calendar dates are fresh) and requires no separate follow-
up. If `refresh` fails, stop and report the error — the rest of this flow
depends on a valid, up-to-date cache.

### 2. Get the event list

Run `paradigm.calendar_days` with `include_rest=true` over a sensible window.
The window should be today through the end of the synced range, or at minimum
today through 28 days ahead (the tool's default). Using the full range that
`refresh` synced (the same default: 30 days back, 180 days ahead) is correct if
you want the whole plan updated; trimming to today onward is fine when only
upcoming dates are relevant to the user's change.

Pass `include_rest=true`. This is not optional. A day that used to have training
and is now a rest day must get an explicit "Rest day" event so the old event is
overwritten, not left stale. The rest-day event carries the same `event_key` and
marker line as a training event; the update path below handles it without any
special case.

Each event in the returned list carries:

- `date` — the calendar date, `YYYY-MM-DD`.
- `title` — the event summary: total duration and session names, or "Rest day".
- `description` — human-readable body with coach notes and session breakdown,
  followed on its own line by a marker of the form
  `[hermes:paradigm:event_key=paradigm-YYYYMMDD]`.
- `event_key` — the stable idempotency key, e.g. `paradigm-20260812`.

### 3. Reconcile each day against the calendar

For each event returned by `calendar_days`, check whether a matching event
already exists on the target calendar for that date.

**Finding an existing event.** Use Hermes' native calendar list/search tool
(the exact tool name should be confirmed against the live Hermes tool registry —
do not assume a specific name). Look for events on the target calendar on that
date. An event belongs to this system if either:

- It has an extended property whose key or value matches `event_key`, or
- Its description contains the marker line
  `[hermes:paradigm:event_key=paradigm-YYYYMMDD]` for that date.

The second check is the fallback and the more portable one: it works even if the
calendar tool does not support extended-property queries. Always check both
before concluding that no owned event exists on a date. Do not touch any event
on that date that does not carry the marker — ownership is the gate.

**If an owned event is found:** update it in place with the new title and
description. Do not create a new event. This handles the rest-day case
automatically: an event that used to say "4h 23m — Ideal Circuit" and now
belongs to a rest day gets updated to "Rest day" with the same `event_key`.

**If no owned event is found:** create a new all-day, transparent (non-busy)
event with the title, description, and `event_key` from the payload. Mark it
`TRANSP:TRANSPARENT` or the equivalent so it does not block the user's calendar
as busy. Use the calendar tool's support for extended properties if available, so
future runs can find the event by key rather than having to scan descriptions.

Use the calendar tool's idempotency or duplicate-detection mechanism wherever
it is available — the goal is exactly one owned event per date.

### 4. Report

After processing all days, report concisely:

- How many events were updated (existed and changed).
- How many were created (new dates with no prior event).
- The date range covered.
- Whether any calls failed, and which dates they affected.

Match the tone of `paradigm.refresh`'s own `summary` field: direct, specific,
no filler. For example:

> Refreshed from Paradigm. Updated 3 events, created 1 new event, spanning
> 2026-08-11 through 2026-09-07. The rest-day event on Aug 11 replaced the
> previous training block.

If any calendar call failed, name the date and the error. Do not silently skip
failed dates or report them as successes.

## What not to do

**Do not fall back to `.ics` import if a calendar tool call fails.** The
`.ics` export path exists as a documented manual fallback for users not running
this through Hermes. It is not a recovery path within this skill. An error from
the calendar tool is the answer to give the user, not a trigger to switch
approaches.

**Do not create duplicate events.** Always check for an existing owned event
before creating a new one.

**Do not touch events you do not own.** Events on the target calendar that do
not carry the `[hermes:paradigm:event_key=...]` marker are not yours. Leave them
entirely alone.

**Do not shift the plan yourself.** If the user says they moved training in the
app, trust that they did. Your job is to pull from Paradigm and reflect what it
says, not to edit the cache or invent date offsets. The plan is already correct
in Paradigm; `refresh` brings the cache in line.

## Tool name note

Hermes' native Google Calendar tool names and signatures are external to this
repo and are not defined here. References above to "the calendar list/search
tool" and "the calendar create/update tool" are intentionally generic. Before
running this flow, confirm the actual tool names against the live Hermes tool
registry (`tools/list` or equivalent) and substitute the real names. Do not
assume names like `gcal_create_event` or `calendar.events.insert` — verify them.

## Manual fallback

If Hermes' calendar tools are unavailable or the user is not running through
Hermes:

```bash
python paradigm_mcp_server.py refresh
python paradigm_mcp_server.py export_ics include_rest=true
```

Then import `training.ics` into Google Calendar via Settings → Import & export.
Re-importing updates existing events by UID and adds new ones; it cannot delete
events, so a day that flipped from training to rest will keep its old event.
Pass `include_rest=true` so that now-rest dates get explicit "Rest day" events
that overwrite the stale ones on re-import.

This is the fallback, not the primary flow.
