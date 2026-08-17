---
name: cron-job-operations
description: "Debug Hermes cronjob timing, output, DM delivery."
version: 1.0.0
author: gladys
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [cron, scheduling, timezone, discord, debugging, homeserver]
    related_skills: [hermes-local-topology, hermes-agent]
---

# Cron Job Operations (this deployment)

## When to Use

Load when creating, editing, debugging, or manually firing a `cronjob` —
especially anything that "didn't fire when expected," fired at the wrong
time, or needs its output inspected after a run. This skill holds
**locally-verified** facts about how the scheduler and delegation-based
manual runs behave here; the generic `hermes-agent` skill covers the cron
*feature* (CLI, schedule syntax) but not this deployment's specific quirks.

## ⚠️ Cron schedules evaluate in server LOCAL time, not UTC

Bare cron expressions (`"0 7 * * *"`) passed to the `cronjob` tool are
evaluated against the server's **local system timezone** (America/New_York
on this deployment), not UTC — even though a naive reading of "standard cron
format" suggests UTC.

**Verified 2026-08-12:** a job's own prompt claimed *"this cron fires at
11:00 UTC = 7:00am Eastern"*. That was wrong. `cronjob(action='list')` showed
`next_run_at: "...T11:00:00-04:00"` and `last_run_at` landing at 11:08am ET on
the nose — the `-04:00` offset is the tell that "11:00" in the expression
was computed as 11:00 **local**. The job had actually been firing at 11am ET
the whole time it was believed to be a 7am job.

**Diagnostic:** always check the UTC offset suffix on `next_run_at` /
`last_run_at` from `cronjob(action='list')`. If the hour in that timestamp
matches the hour in the cron expression, the scheduler is using local time —
don't layer a manual UTC conversion on top of it when writing the schedule.

**Pitfall to avoid:** don't write "fires at HH:MM UTC = HH:MM local" as a
self-reminder inside a job's own prompt text. It drifts out of sync the
moment the schedule field is edited without updating the prose, and a future
session (or subagent running the job) will trust the stale comment over the
actual `schedule` field. If the fire time needs documenting for a human
reader, state it plainly as local time with no conversion claim, or omit it
and let `cronjob(action='list')` be the source of truth.

## ⚠️ Cron output files are not reachable from the container

Each cron run writes a full transcript to
`C:\Users\<user>\AppData\Local\hermes\cron\output\<job_id>\<timestamp>.md` on
the Windows host. That path is **not under any container mount** — not
`/sandbox`, not `/root` (a *different* 9p share — the sandbox-local home,
despite superficially resembling `C:\`), not `/workspace`. Searching for it
via `search_files`/`read_file` (or raw shell `find`) from inside the container always fails,
silently burning several tool calls per attempt (verified across separate
sessions on 2026-08-11 and 2026-08-12 — same dead end hit twice).

**What actually works:** the `[ASYNC DELEGATION COMPLETE]` message you get
back from `cronjob(action='run')` already includes an inline preview of the
job's output (head-truncated). For the full transcript, use `session_search`
for the cron run's own session — cron runs get their own session named
`cron_<job_id>_<timestamp>` — rather than chasing the host-side `.md` file
from a container tool call.

## Manual runs vs. direct-trigger timeouts

`cronjob(action='run', job_id=...)` executes the job in the background via
delegation and returns immediately — the parent conversation is not blocked
waiting on it, so it isn't subject to any synchronous request-timeout
ceiling. If a user reports a job timing out at a fixed ceiling (~150s
observed) when triggered some other way (e.g. directly through a Discord
interaction/webhook path), that ceiling belongs to whatever synchronous path
they used to trigger it, not to `cronjob`'s own execution. Re-firing via
`cronjob(action='run')` from a chat session is the reliable way to manually
test/re-send a job on demand.

## Discord DM delivery pattern (dual jobs)

When a cron job must DM a specific person rather than post to a channel, the
working pattern on this deployment is: give the job the
`mcp-discordmessenger` toolset, set `deliver: local` (suppresses default
cron delivery), and have the job's own prompt explicitly call
`mcp__discordmessenger__discord_dm` with a named user — never rely on cron's
built-in delivery target for a DM, since that path resolves through a
channel ID and breaks the moment that channel/ID changes (`Unknown Channel`
404). Keep the prompt's delivery section explicit that the tool must be
called **exactly once** after the full message is composed — composing
live and sending partial drafts followed by a "corrected" resend results in
duplicate, inconsistently-formatted messages landing in the same DM.
