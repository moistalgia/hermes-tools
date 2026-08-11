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

Everything goes through the **`paradigm` MCP server** and Hermes' native Google
Calendar import capability. Those are the only two supported paths. If a tool
call fails, the fix is never a different transport — surface the error and stop.

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
variable on the `paradigm` server. Its default is `Climbing`. Use the value
from the server's `refresh` or `export_ics` response, not a hardcoded string.

## Flow

### 1. Refresh from Paradigm

Run `paradigm.refresh` with `include_rest=true`. This re-pulls from Paradigm,
updates the cache, and writes a fresh `training.ics` — all in one step.

`include_rest=true` is not optional. A day that used to have training and is now
a rest day must appear in the `.ics` with its own event (titled "Rest day") so
that Google updates the existing event in place rather than leaving the stale
training block behind. The `.ics` event UIDs are derived from the date
(`paradigm-YYYYMMDD@hermes-tools`), so Google's import reconciles by UID: if an
event for that date already exists it is updated, if not it is created. That
covers every case — including the rest-day flip — without any agent-side
reconciliation logic.

If `refresh` fails, stop and report the error. Everything else depends on a
valid, up-to-date `.ics`.

### 2. Import the `.ics` into Google Calendar

Use Hermes' native Google Calendar import tool to push `training.ics` into the
target calendar. The file path is returned by `refresh` (and also by
`export_ics`) in the `path` field of the response.

Google's import API reconciles by `UID`: events that already exist are updated,
new dates get created. No agent-side diffing, no ownership markers, no
list-then-match loop needed — Google does the work.

The exact name of Hermes' calendar import tool is external to this repo. Confirm
it against the live Hermes tool registry (`tools/list` or equivalent) before
running. Do not assume a specific tool name.

### 3. Report

After the import call returns, report concisely:

- That the plan was refreshed from Paradigm and pushed to the `Climbing`
  calendar (or whatever `PARADIGM_CALENDAR_NAME` resolves to).
- The date range covered, taken from `refresh`'s `summary` field.
- If the import call failed, name the error verbatim. Do not retry silently.

Match the tone of `paradigm.refresh`'s own `summary`: direct, specific, no
filler. For example:

> Refreshed from Paradigm and imported into Climbing. Plan covers
> 2026-08-11 through 2027-02-07; rest days are now explicit so any stale
> training blocks are overwritten.

## What not to do

**Do not shift the plan yourself.** If the user says they moved training in the
app, trust that they did. Your job is to pull from Paradigm and reflect what it
says.

**Do not walk through `calendar_days` and call create/update per event.** The
import API handles the full batch in one call; per-event tool calls are slower,
fragile, and unnecessary here.

**Do not retry a failed import by switching to a different transport.** An error
from the import tool is the answer to give the user.
