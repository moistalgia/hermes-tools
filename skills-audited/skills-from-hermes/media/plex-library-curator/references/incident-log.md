# Incident Log

## Original pitfall text, kept for history: `discover`'s 25-item cap

Before `library_export` existed (added 2026-08-15), the only enumeration path
was `discover`, and it silently capped at ~25 items per call regardless of
requested limit — verified live 2026-08-15 pre-export-tool: `limit=501` on an
unfiltered call still came back `count: 25`. This was not a Plex Media Server
ceiling; the underlying API supports real pagination via
`X-Plex-Container-Start`/`Size`, `discover` just didn't expose it. That gap is
what `library_export` now closes — the current hot-path rule ("use
`library_export` for full-inventory questions, not `discover`") supersedes
this entirely; `discover` is still capped and still the wrong tool for
enumeration, but it's no longer the only option.

## The 2026-08-14 unbounded delegation failure

A live test, 2026-08-14: a leaf subagent with exactly the right invocation
path ran ~59 minutes on per-year `discover` slices (the year filter worked
and each small slice came back complete), consumed its entire API-call budget
at ~19 of ~50 needed calls, then hit max-iterations — **and even its final
answer was lost: the non-streaming call timed out at 180s while composing.**
Everything learned in that run died with it.

Consequences drawn from this:
- Never sell a "background full census" as an easy win; fast tasks use
  `library_overview` counts + targeted probes only.
- If a complete title inventory is genuinely wanted, the fix is structural —
  a host-side export endpoint on the Plex MCP that dumps all items to disk in
  one call (needs the owner's OK, since it touches his server) — not another
  delegate attempt.
- Any delegate that IS run this way should stay under ~15 tool calls and
  report partial results rather than chase completeness.

`library_export` (added the next day, 2026-08-15) closes much of this gap for
direct calls, and per the Batch/continuous curation section, a large *bounded*
batch delegation (stated count/budget, `library_export` + `check_titles` in
bulk) is now more defensible than it was here — but this specific failure mode
(open-ended "enumerate everything" handed to a subagent) is exactly what to
avoid regardless of which tools are available.

## MCP outage mid-sweep

During the 2026-08-14 run, `mcp__plex__` went unreachable after consecutive
failures ("auto-retry available in ~60s"). Correct handling, verified: stop
hammering it, wait ~65s (one terminal sleep), resume — but each outage call
still burns budget against whatever iteration ceiling is in force.
