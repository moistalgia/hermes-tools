---
name: plex-library-curator
description: Audit the Plex library, find gaps, propose defensible adds.
version: 0.1.0
author: moisty, Gladys (Hermes Agent)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [plex, curation, collection-building, recommendations, mcp]
    related_skills: [plex-media-playback, web-access]
---

# Plex Library Curator

Acquisition-side curation for the house Plex server: census the library, infer
what its owner actually likes, locate structural holes, and propose titles to
**add** — each one carrying a warrant that survives a "why?".

**The end goal is a massive, curated collection of media. Simple as that.**
Gaps and holes are one path to that goal, not the whole of it — a genuinely
great film is a legitimate add on its own merit, with no other warrant needed,
because "this is excellent and isn't here yet" directly serves the goal. What
counts as an acceptable warrant depends on what moisty actually asked for: a
"find the gaps" request wants structural/canon/lineage reasoning; a "just find
me great stuff to add" request wants quality-driven picks and the stricter
warrant-typing below doesn't apply to it. Read the request before assuming
which mode you're in.

This is the inverse of `plex-media-playback`. That skill's hard rule is *never
name a title you haven't seen in a tool result*. Here the entire deliverable is
titles that are **not** on the server, so that rule is replaced by a stricter
one: every proposed title must be **verified absent** via `mcp__plex__search`
before it reaches the user, and every claim about the library must come from a
tool result.

Not a "more of what you already have" engine. Volume in the library is not
evidence of appetite — see [The horror correction](#the-horror-correction).

## When to Use

- "What should I add to the library?" / "find gaps" / "what am I missing?"
- Planning an acquisition batch, an event slate, or a themed block
- Periodic library review (quarterly is a sane cadence)
- Don't use for: playing something, or recommending from what's already there —
  that's `plex-media-playback`.

## Prerequisites

- The `plex` MCP server, exposed as `mcp__plex__*`. It runs host-side; there is
  no in-container fallback. If the tools don't resolve, load the
  `plex-media-playback` skill and follow its MCP readiness guide — then stop.
- `web_search` / `web_extract` for external canon and release data. Never
  substitute recall for a lookup — a canon list quoted from memory is exactly
  the failure mode this skill exists to prevent.
- Write access to `/sandbox/out` for the report.

## The three rules

1. **Every pick carries a warrant.** A warrant names specific evidence — a
   library title, a census fact, a canon source. "You'd like it" is not a
   warrant. "You have *Prisoners* and *Sicario* but no *Zodiac*" is.
2. **No forced picks.** Slots may come back empty. If a gap has no candidate
   you can defend, write "no defensible candidate" and move on. A short honest
   list beats a padded one, and padding is the one thing that makes the whole
   report untrustworthy.
3. **Verified absent, or it isn't a pick.** `mcp__plex__search` every candidate
   title before proposing it. Search is fuzzy, so a genuine miss means absent;
   a hit means drop the pick silently.

## Warrant types (the only acceptable ones for gap-analysis mode)

The table below governs **gap-analysis requests** ("what am I missing", "find
holes", periodic review). It does not govern **quality-driven requests** ("just
find great stuff to add", "curate for a while", open-ended batch curation) —
see [Quality mode](#quality-mode) immediately after. If moisty didn't ask for a
gap analysis specifically, default to quality mode; it's the more natural fit
for "build me a massive, great collection."

| Type | Shape | Example |
| --- | --- | --- |
| **Canon** | Consensus all-timer, absent. Cite the list. | "Top-10 on Sight & Sound 2022; the library has no Kubrick at all." |
| **Bridge** | ≥2 library titles make it a near-certainty. Name them. | "You have *Annihilation* and *Arrival*; *Solaris* (1972) is their shared ancestor." |
| **Lineage** | You hold the descendant, not the ancestor — or part of a set. | "You have *Alien: Romulus* and no *Aliens*." / "Two-thirds of a trilogy." |
| **Coverage** | A whole mode of cinema is at or near zero. Structural, not taste. | "Zero documentaries in Movies (verified: `discover` returns nothing)." |
| **Event** | Serves the horror event as *programming*, not as more horror. | "Fills the 'crowd-pleaser opener' slot; the slate is all slow-burn." |

**Never acceptable in gap-analysis mode:** genre-volume inference ("you own 90
horror films, here's number 91"), vibes, box office, "trending now", awards as
a lone reason, or any pick whose warrant you cannot trace to a named title,
census number, or cited source. If you catch yourself writing "you'd probably
enjoy", delete the pick.

Rating a pick's strength is fine and useful: **strong** (warrant is airtight),
**worth a look** (defensible but arguable). There is no third tier — if it's
weaker than "worth a look" it doesn't ship, in gap-analysis mode.

## Quality mode

For "find great stuff to add", "curate for a while", or any open-ended batch
run: the warrant can simply be **acclaim + absence**. No structural connection
to the existing library is required. Still ground every pick in something
real:

- A cited critical consensus (Sight & Sound, Criterion, TSPDT 1000, Letterboxd
  Top 250, a respected best-of-decade/genre list, a notable festival/awards
  win) or a specific, named reason it's excellent — not "you'd like this."
- Verified absent via `check_titles`/`search`, same as gap mode.
- Still run the horror correction — quality mode is not an excuse to dump more
  horror into a library that already has plenty; if a pick is horror it should
  clearly justify itself against the *event* framing, not just "it's acclaimed
  horror."
- Diversity is still a soft goal even without a structural gap driving it —
  spread picks across decades/genres/countries rather than clustering on
  whatever one canon list you happened to pull first.

Quality-mode picks don't need the strong/worth-a-look labels — just the source
of acclaim, one line, per pick.

## Batch / continuous curation

When asked to "just curate for a while" rather than answer one question, this
is a long-running batch job, not a single-shot report:

1. Decide scope up front: a target count (e.g. "150 titles"), a time/call
   budget, or both. State it before starting so there's a stopping condition.
2. Work in passes across different canon sources / genres / decades / national
   cinemas so the batch doesn't collapse into one list's ordering (e.g. don't
   just walk the AFI 100 top-to-bottom and call it done — mix in Sight & Sound,
   genre-specific canons, a few national-cinema lists, and structural gaps from
   `find_gaps`/`library_stats`).
3. Batch the absence check: accumulate candidates and run them through
   `mcp__plex__check_titles` in large groups rather than one at a time — it's
   built for exactly this.
4. Write incrementally to the output file rather than holding the whole batch
   in context until the end — append sections as passes complete, so a
   long-running job that gets interrupted still leaves a partial, usable
   report on disk.
5. This is a case where delegating to a subagent is more defensible than the
   single-title lookups warned about elsewhere in this skill — a large batch
   with a stated count/budget and instructions to use `library_export` +
   `check_titles` in bulk (not `discover` looped per genre) is bounded work,
   unlike the open-ended "enumerate everything" delegation that failed in
   2026-08-14. Give it the scope, the sources to pull from, and the output
   path; don't give it an unbounded "find everything good."

## Procedure

**Run this directly, in the main conversation. Do not delegate the census (or the
skill as a whole) to a subagent.** A live-run 2026-08-15 confirmed the entire
audit — census, genre probes, director probe, two absence searches, canon
lookup — completes in well under a minute of direct tool calls. The 2026-08-14
failure came from delegating an unbounded "enumerate everything" task to a
subagent, which then burned its whole budget on sequential `discover` slices
and died mid-flight (see Pitfalls). Delegate only if you truly need to keep a
huge raw dump out of the main context — and even then, cap it at a named list
of specific probes, never "get the full inventory."

1. **Census.** As of 2026-08-15 the Plex MCP got a real bulk-export tool —
   `mcp__plex__library_export` — which returns the **complete contents of a
   library in one call**, not a page of 25. Use `detail='minimal'` for a
   ~500-title inventory at a few thousand tokens; only raise detail on a
   narrowed subset. **This replaces the old genre-by-genre `discover` sweep
   below — do not loop `discover` to enumerate a library anymore.** Also call
   `mcp__plex__library_stats` for counts by decade/genre/resolution/rating/
   watched-state and disk usage before hunting for gaps — it shows where the
   collection is thin without listing a single title. `library_overview` is
   still useful for the exact genre/decade vocabulary (a genre absent from the
   list means zero items — verified) and `list_libraries` for a bare section
   list.
   *Done when:* you have a full-library export plus per-genre/decade stats for
   both libraries, all from tool output.
2. **Model the taste, not the volume.** Weight *watched* items far above merely
   present ones — `discover` returns a `watched` flag per item. Look for
   repeated directors, repeated writers/DPs, recurring structural preferences
   (practical effects, long takes, unreliable narrators, ensemble crime), and
   which genres show high watched-ratios versus which are stockpiled and
   untouched. Write the model down as 4-8 explicit claims before proposing
   anything.
   *Done when:* each claim cites the titles that support it.
3. **Apply the horror correction** (next section).
   *Done when:* horror is excluded from the taste model's weighting and handled
   as its own separate slate.
4. **Find the holes.** Three passes:
   - *Structural* — genres at zero or near-zero, decades at zero, no
     international cinema, no documentary, no silent/classical era. Also run
     `mcp__plex__find_gaps` — it computes TV seasons with missing episodes,
     movies stuck at low resolution, and items with broken/absent metadata
     directly from server state (no guessing about what "should" be there).
     That's a different kind of hole than acquisition gaps — it's about fixing
     what's already partly there, not proposing new titles — but it belongs in
     the same report if this is a periodic review.
   - *Canon* — pull real lists with `web_search`/`web_extract`: Sight & Sound
     (critics + directors), AFI 100, Criterion, TSPDT 1000, Letterboxd Top 250,
     and per-genre canons. Diff against the census.
   - *Lineage* — incomplete trilogies/franchises, originals missing behind
     remakes, a director represented by one film when the library's own logic
     wants three.
   *Done when:* every hole is stated as a fact with the tool call or URL behind it.
5. **Verify absence.** Use `mcp__plex__check_titles` to check a whole candidate
   list against the library in one call — it reports present / missing / too
   close to call, so there's no more need to `search` one title at a time. Any
   "too close to call" result still needs a manual `search` to resolve before
   the pick ships.
   *Done when:* no unverified title remains on the list.
6. **Write the report.** Follow
   [templates/report-template.md](templates/report-template.md). Full report to
   `/sandbox/out/plex-curation-<YYYY-MM-DD>.md`, attach it with `MEDIA:`, and
   put a short shortlist in chat — top picks with one-line warrants, plus the
   count of what's in the file.
   *Done when:* the chat message is short and the file is complete.

## The horror correction

The library skews hard toward horror because moisty **hosts a horror event**.
That is a programming obligation, not a preference weight. Treated naively, the
volume swamps every similarity metric and the curator degenerates into a horror
recommender.

Handling:

- **Exclude horror volume from the taste model entirely.** When counting genre
  affinity in step 2, drop Horror-tagged items from the denominator and the
  numerator. Infer taste from everything else.
- **Horror gets its own slate**, judged as *programming*: pacing variety across
  a night, crowd-pleaser vs. slow-burn balance, decade spread, subgenre spread,
  runtime fit. A great horror pick here fills a **slot in a lineup**, not a
  genre bucket.
- **Cross-genre reads must survive the horror strip.** If a bridge warrant only
  holds because two horror films share a tag, it isn't a warrant. Re-derive it
  from non-horror titles or drop it.
- **Do not treat the non-horror library as small or unserious.** It is where the
  actual signal lives, and it's the side with the real growth headroom.

## Pitfalls

- **`decade` rejects "1990s".** Despite `library_overview` reporting decades as
  `"2010s"` and `discover`'s own parameter docs, Plex errors with *Invalid value
  "2010s" for filter field "decade", value should be type integer*. Pass a bare
  year (`decade='1994'`) — the tool maps it to `year`. Verified failure; don't
  retry the string form. To get decade coverage, iterate years or sort by year.
- **"Nothing in the library matches those filters" is a success, not an error.**
  It's the tool confirming a zero — that's a coverage finding. Record it.
- **`library_overview`'s Movies genre list has no Documentary.** Confirmed by a
  zero-result `discover`. That is a real, large hole.
- **No collections exist on this server** and Plex's own `similar` field is
  empty for every item. Don't build the report around either.
- **`similar_to` ranks by shared genre tags only.** It's a coarse in-library
  tool; it cannot see anything you don't own, so it's useless for acquisition.
  Use it to check whether a "gap" is already covered by something adjacent.
- **Don't quote counts from memory or from an earlier session.** The library
  grows. Re-run `library_overview` every time.
- **RESOLVED 2026-08-15: `library_export` now exists and does full-library
  enumeration server-side in one call.** The old pitfall below (discover capping
  at ~25 items/call, no offset/container_start) is now historical — it
  described `discover`, which is still capped and still the wrong tool for
  enumeration, but `library_export` is the dedicated bulk-export tool the
  skill used to say was missing. Use `library_export` for any "what's the full
  inventory" question; keep `discover` for filtered/open-ended requests only.
  A full title-level census may now be viable in a single delegated task where
  it wasn't before (2026-08-14 failure below predates this tool) — but confirm
  with a live retest before promising that; the token cost of a 500-title
  export at higher `detail` levels is still real and `detail='minimal'` should
  be the default.
  <details>Original pitfall text, kept for history: each `discover` call
  returns at most ~25 items regardless of requested limit (verified live
  2026-08-15 pre-export-tool: `limit=501` on an unfiltered call still came back
  `count: 25`) — not a Plex Media Server ceiling, the underlying API supports
  real pagination via `X-Plex-Container-Start`/`Size`, `discover` just didn't
  expose it. That gap is what `library_export` now closes.</details>
- **Delegated subagents DO inherit the Plex MCP tools — but only as *deferred*
  tools.** `mcp__plex__*` won't appear in a child's visible toolset, so a child
  that introspects its own tool list will wrongly report Plex as unavailable.
  Tell it explicitly to go `tool_search(query='plex')` → `tool_describe` →
  `tool_call(name='mcp__plex__...')`. Verified: a leaf subagent called
  `library_overview` against the live host-side server successfully. Delegating
  is therefore viable — but see next pitfall before promising it work it can't do.
- **A full title-level census does NOT fit inside one delegated task.** Live
  test, 2026-08-14: a leaf subagent with exactly the right invocation path ran
  ~59 minutes on per-year `discover` slices (the year filter works and each
  small slice came back complete), consumed its entire API-call budget at ~19 of
  ~50 needed calls, then hit max-iterations — **and even its final answer was
  lost: the non-streaming call timed out at 180s while composing.** Everything
  learned in that run died with it. Consequences: (a) never sell a "background
  full census" as an easy win; fast tasks use `library_overview` counts +
  targeted probes only, exactly as written in step 1; (b) if a complete title
  inventory is genuinely wanted, the fix is structural — a host-side export
  endpoint on the Plex MCP that dumps all items to disk in one call (needs the
  owner's OK, it touches his server), not another delegate; (c) instruct any
  delegate that IS run this way to stay under ~15 tool calls and report partial
  results rather than chase completeness.
- **MCP can drop mid-sweep.** During the same run `mcp__plex__` went unreachable
  after consecutive failures ("auto-retry available in ~60s"). Correct handling,
  verified: stop hammering it, wait ~65s (one terminal sleep), resume — but each
  outage call burns budget against whatever iteration ceiling you have.

## Verification

- Every library claim in the report traces to a tool result in this session.
- Every proposed title has a recorded `mcp__plex__search` miss.
- Every pick has one of the five warrant types, explicitly labelled.
- No pick's warrant depends on horror volume.
- At least one section says "no defensible candidate" if that's the truth.
- Report exists at `/sandbox/out/plex-curation-<date>.md` and is attached.
