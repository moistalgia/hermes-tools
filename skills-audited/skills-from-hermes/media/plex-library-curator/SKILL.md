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
is a long-running batch job, not a single-shot report. The title count itself
is cheap — `check_titles` returns almost nothing per title — so the actual
budget risk lives in two other places: re-fetching canon sources, and how the
report file gets written across passes. Both have a specific fix below; don't
substitute a vaguer version of either.

1. Decide scope up front: a target count (e.g. "150 titles"), a time/call
   budget, or both. State it before starting so there's a stopping condition.
2. **Cache canon sources instead of re-fetching them live.** AFI 100, Sight &
   Sound, TSPDT 1000 and similar lists don't change week to week, so a
   `web_search`/`web_extract` pull is a one-time cost, not a per-run one.
   Before fetching a source, check `references/canon-lists/<source>.md` in
   this skill's own folder. If it exists and is under ~90 days old, read that
   instead of hitting the web. If it's missing or stale, fetch it, reduce it
   immediately to just `title (year)` pairs — discard the rest of the page,
   don't keep the full extracted article in context — and write that reduced
   list to `references/canon-lists/<source>.md`, overwriting the old one. This
   turns a 10-30K-token research cost into a one-time quarterly cost instead of
   something paid on every run.
3. Work in passes across different canon sources / genres / decades / national
   cinemas so the batch doesn't collapse into one list's ordering (e.g. don't
   just walk the AFI 100 top-to-bottom and call it done — mix in Sight & Sound,
   genre-specific canons, a few national-cinema lists, and structural gaps from
   `find_gaps`/`library_stats`).
4. Batch the absence check: accumulate candidates and run them through
   `mcp__plex__check_titles` in large groups rather than one at a time — it's
   built for exactly this.
5. **Write incrementally by true append, never by read-modify-write.** Do not
   use a generic file-write/patch tool that reads the current file before
   rewriting it — on a growing report, that means every later pass re-loads
   everything every earlier pass wrote, and cost climbs with the square of the
   pass count instead of staying flat. Instead:
   - Write report sections 1-3 (census, taste model, gaps) once, near the
     start, and don't revisit them.
   - For section 4 (Picks), use `terminal` to genuinely append each pass's new
     entries — e.g. `printf '%s\n' "<pass output>" >> /sandbox/out/plex-curation-<date>.md`
     — which touches only the bytes being added, never the bytes already
     there.
   - If a true append isn't available in whatever path is running this,
     write each pass's picks to its own small file instead
     (`picks-pass-1.md`, `picks-pass-2.md`, ...) in a scratch dir, and do one
     final `cat` to assemble them into the report at the end. Either way, no
     pass should ever need to read what an earlier pass wrote.
   - The point of both: a long-running job that gets interrupted still leaves
     a partial, usable report on disk, and no single pass's cost grows just
     because earlier passes happened.
6. Delegating a large, bounded batch to a subagent is fine here — give it a
   stated count/budget and tell it to use `library_export` + `check_titles` in
   bulk (not `discover` looped per genre). Never delegate an open-ended
   "find everything good"; see `references/incident-log.md` for why that fails.

## Procedure

**Run this directly, in the main conversation — do not delegate the census
(or the skill as a whole) to a subagent.** The full audit (census, genre
probes, director probe, absence searches, canon lookup) completes in well
under a minute of direct tool calls. Delegate only if you truly need to keep
a huge raw dump out of the main context — and even then, cap it at a named
list of specific probes, never "get the full inventory" (see
`references/incident-log.md` for what an unbounded delegated census costs).

1. **Census.** `mcp__plex__library_export` returns the **complete contents of
   a library in one call**, not a page of 25 — use `detail='minimal'` for a
   ~500-title inventory at a few thousand tokens, and only raise detail on a
   narrowed subset. **This replaces the old genre-by-genre `discover` sweep —
   do not loop `discover` to enumerate a library.** Also call
   `mcp__plex__library_stats` for counts by decade/genre/resolution/rating/
   watched-state and disk usage before hunting for gaps — it shows where the
   collection is thin without listing a single title. `library_overview` is
   still useful for the exact genre/decade vocabulary (a genre absent from the
   list means zero items) and `list_libraries` for a bare section list.
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
   - *Canon* — check `references/canon-lists/<source>.md` first (see
     [Batch / continuous curation](#batch--continuous-curation) for the
     caching rule); only `web_search`/`web_extract` a source if its cache is
     missing or stale. Sources: Sight & Sound (critics + directors), AFI 100,
     Criterion, TSPDT 1000, Letterboxd Top 250, and per-genre canons. Diff
     against the census.
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
  year (`decade='1994'`) — the tool maps it to `year`. Don't retry the string
  form. To get decade coverage, iterate years or sort by year.
- **"Nothing in the library matches those filters" is a success, not an error.**
  It's the tool confirming a zero — that's a coverage finding. Record it.
- **`library_overview`'s Movies genre list has no Documentary.** A zero-result
  `discover` confirms it. That is a real, large hole.
- **No collections exist on this server** and Plex's own `similar` field is
  empty for every item. Don't build the report around either.
- **`similar_to` ranks by shared genre tags only.** It's a coarse in-library
  tool; it cannot see anything you don't own, so it's useless for acquisition.
  Use it to check whether a "gap" is already covered by something adjacent.
- **Don't quote counts from memory or from an earlier session.** The library
  grows. Re-run `library_overview` every time.
- **Use `library_export` for full-inventory questions, not `discover`.**
  `discover` caps at ~25 items per call regardless of requested limit and
  exposes no pagination — fine for filtered/open-ended browsing, wrong tool
  for "what's the full inventory." `library_export` returns the complete
  library in one call; default to `detail='minimal'`, raise detail only on a
  narrowed subset.
- **Delegated subagents DO inherit the Plex MCP tools — but only as
  *deferred* tools.** `mcp__plex__*` won't appear in a child's visible
  toolset, so a child that introspects its own tool list will wrongly report
  Plex as unavailable. Tell it explicitly to
  `tool_search(query='plex')` → `tool_describe` → `tool_call(name='mcp__plex__...')`.
- **Cap any delegated census-style task at ~15 tool calls with a stated
  scope — a full title-level census does not fit inside one delegated task.**
  Fast tasks use `library_overview` counts + targeted probes only, exactly as
  in Procedure step 1. If a complete title inventory is genuinely wanted, the
  durable fix is a host-side export endpoint dumping all items to disk in one
  call (needs the owner's OK), not a delegate run to exhaustion. See
  `references/incident-log.md` for what happens when this is ignored.
- **MCP can drop mid-sweep.** If `mcp__plex__` calls start failing
  consecutively ("auto-retry available in ~60s"), stop hammering it, wait
  ~65s (one terminal sleep), then resume — each outage call still burns
  budget against whatever iteration ceiling you have.

## Verification

- Every library claim in the report traces to a tool result in this session.
- Every proposed title has a recorded `mcp__plex__search` miss.
- Every pick has one of the five warrant types, explicitly labelled.
- No pick's warrant depends on horror volume.
- At least one section says "no defensible candidate" if that's the truth.
- Report exists at `/sandbox/out/plex-curation-<date>.md` and is attached.
