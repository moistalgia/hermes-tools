---
name: plex-acquire-movie
description: Grab a movie via Plex, Prowlarr, qBittorrent download.
version: 0.1.0
author: Gladys (Hermes Agent)
license: MIT
metadata:
  hermes:
    tags: [plex, prowlarr, qbittorrent, mcp, acquisition]
    related_skills: [plex-library-curator, plex-media-playback]
---

# Plex Acquire Movie

End-to-end flow: pick or receive a title -> verify it's actually missing ->
find a good torrent -> send it to qBittorrent. Three MCP servers, no hard
logic beyond a size/seed tradeoff call.

## When to use

- "Hermes, grab <movie> for me" — explicit title given, skip to step 2.
- "Hermes, find me something new for tonight" — no title given, do step 1
  first (recommendation), then fall into the same acquisition pipeline.
- Not for TV shows (untested — likely needs season-pack logic) and not for
  playback (`plex-media-playback` handles "put X on the TV").

## Prerequisites

Three host-side MCP servers, exposed as `mcp__plex__*`, `mcp__prowlarr__*`,
`mcp__qbt__*`. If any tool name doesn't resolve, don't hand-roll a curl/API
workaround — report it and stop. Run `qbt_status` / `prowlarr_status` /
`plex_status` first if anything seems off; they diagnose reachability vs.
credential vs. config problems.

## Procedure

1. **Pick a title (only if none was given).**
   - `mcp__plex__library_overview` then `discover` (e.g. `min_rating=7.5,
     sort=rating`) or `on_deck` to see what's already in flight, to seed ideas.
   - Use a real warrant, not vibes — see `plex-library-curator` for the full
     warrant taxonomy (canon / bridge / lineage / coverage / event). A quick
     version for this flow: name 1-2 titles already in the library that make
     the pick an obvious next step.
   - **Verify absence** with `mcp__plex__search`. Try 2-3 phrasings (title
     alone, title+year, title+director) before concluding it's really
     missing — search is fuzzy and a single miss isn't proof. A hit means
     drop it and pick something else.

2. **Search Prowlarr — bare title first, sort by seeders, indexer=1337x.**
   **Default query is the bare title alone — no year, no resolution
   qualifier.** `mcp__prowlarr__search(query="<Title>", indexer="1337x",
   sort="seeders")`. This is the procedural fix for a root cause that bit
   twice in one session (2026-08-15, both below): a *qualified* query (a
   year, a foreign-language alias) can be wrong in ways you have no way to
   detect from inside that one query, and a thin/empty result from a
   qualified search silently looks like "nothing better exists" instead of
   "the qualifier was wrong." Concretely:
   - **Start bare.** Bare-title, seeder-sorted results surface the
     best release regardless of what year or alias the indexer filed it
     under — it's the query with the fewest assumptions baked in.
   - **Add a year only to cut noise**, if the bare search is swamped with
     unrelated junk (common single-word titles pull in shorts, or — as with
     "Solaris" — unrelated adult content; add the year at that point purely
     as a filter). Never let the year-qualified search be the *only* one you
     run.
   - **If a qualified search comes back thin or empty, that means "drop the
     qualifier," not "try a different qualifier."** Don't reach for a second
     guess (alternate spelling, director's name) before first re-running the
     plain bare-title search sorted by seeders — that alone resolves both
     failure modes below directly.
   - **Sanity-check seeder count against the title's fame.** A canon/notable
     film returning only single-digit seeders on your first search is itself
     a signal something was missed, not a conclusion that the swarm is just
     thin — recheck with a bare search before accepting it.
   - Always scope to `indexer="1337x"` explicitly for movies — BroadcasTheNet
     is TV-only (see Pitfalls). Returns pre-parsed results with resolution,
     codec, size, seeders, age, and a ready `magnet` field.

2a. **Batch acquisition (multiple titles in one run) — control context, don't
    let it control you.** A 10-movie run means 10 Prowlarr searches, and each
    raw result carries full tracker lists (15-20 `tr=` entries per magnet,
    repeated three times: `magnet`, `download_url`, `fetch_command`). Left
    unbounded this is what caused a real session to blow its context
    (2026-08-16) before a single download fired. Fix, in order of impact:
    - **Cap `limit` to 5 per search, always.** You only need to see the top
      handful to make a seeders-vs-resolution call — you never need 30+ raw
      releases in context. `limit=5` was used across a verified 10-title batch
      and every pick was still correct.
    - **Always pass `indexer="1337x"` explicitly** (see Pitfalls) — this alone
      roughly halves the raw payload per call by skipping the multi-indexer
      merge/dedup step and avoids the private-tracker timeout risk entirely.
    - **Fire independent title searches in parallel, one batch of tool calls
      per turn, not one search per conversational turn.** Ten titles that
      don't depend on each other's results should be ten `mcp__prowlarr__search`
      calls issued together — this keeps the back-and-forth from repeatedly
      resending the whole growing conversation. Follow immediately with the
      matching `mcp__qbt__download` calls for whichever titles came back clean,
      also batched.
    - **After picking a release, quote only the fields that matter in your own
      output/summary** (title, resolution, seeders, size) — never restate the
      full magnet/tracker block in prose; pass the `magnet` field straight
      from tool output to `mcp__qbt__download` without echoing it.
    - **Don't re-run a search "just to double check" once you have a clean
      hit.** Prowlarr results are ranked and live; re-querying the same title
      a second time without new information (a failed download, a wrong-film
      match) just duplicates payload for no gain.
    - **Budget one retry per title, not a search spiral.** If bare-title
      returns wrong-film noise (see Pitfalls) or a year-qualified search comes
      back empty, one corrective re-query (bare + a plain-string year, per the
      wrong-film pitfall below) is enough — if that also fails, stop and flag
      the title to the user rather than trying five spelling variations.

3. **Pick a release: highest seeders wins, 1080p, no 720p, size is a
   secondary signal not a hard filter.**
   - **Default policy, per moisty: take the highest-seeder 1080p release,
     full stop — don't downgrade to 720p to chase a smaller file size.** Size
     preference (he aims for 0.5-4GB and has noted smaller files in that band
     often carry more seeders anyway) is a helpful heuristic for guessing
     which release *might* be well-seeded, not a constraint that overrides
     seeder count once you can actually see the numbers. If the highest-seeder
     1080p release is outside 0.5-4GB (e.g. a 11.8GB remux with more seeders
     than a 2GB encode), that's worth a one-line flag in your summary, but it
     doesn't disqualify the pick — seeder count is the deciding signal.
   - **720p is off the table** unless every 1080p option is confirmed dead
     (all attempted, all stalled at 0 seeders) and the user is asked first —
     don't auto-fall-back to 720p silently.
   - Skip anything flagged `cam:true`.
   - Below ~5-10 seeders, expect a slow/stalled pull regardless of quality —
     flag that risk in your summary rather than silently picking it if a
     healthier option exists.

4. **Download.** `mcp__qbt__download(magnet=<magnet field verbatim>,
   name="<Title> (<Year>)")`. The tool auto-routes by detected category to
   `movies_path`/`shows_path` from `qbt_status` — `category`/`name` args are
   often ignored (`ignored_arguments` in the response will say so; that's
   fine, routing still worked). Check `response.result.confirmed == true` and
   `save_path` matches the movies path.

5. **Verify it's actually moving.** `mcp__qbt__downloads` — confirm `state`
   is `downloading` (not stuck on `metaDL`/`stalledDL`), `seeders > 0`, and
   `speed_mbps > 0`. `metaDL` right after adding is normal for a few seconds
   (fetching torrent metadata from the swarm); if it's still `metaDL` on a
   second check, the magnet's trackers may be dead — try a different release.
   **Check twice, a few seconds apart, before judging health.** The very
   first `downloads` read after adding can show `state: downloading` but
   still `seeders: 1, speed_mbps: 0.0`, and an absurd `eta_minutes` (tens of
   thousands) — that's the client still connecting to peers, not a dead
   swarm. Don't report that first snapshot as a failure or swap releases on
   it; re-check once more before concluding the swarm is actually thin.

## Pitfalls

- **Root cause behind two separate misses in one session (2026-08-15): a
  *qualified* search query (year, or a foreign-language alias) was trusted as
  exhaustive when it was actually the source of the miss.** See step 2 above
  for the fix (bare-title-first, qualify only to cut noise). Evidence:
  - *North Face (2008)*: searching "Nordwand 2008" (the German alias) returned
    only 4 weak/old results (best: 5 seeders, 11.8GB); the actual best release
    was titled plainly "North Face (2008) [...] [YTS] [YIFY]" with 121
    seeders at 2.3GB — invisible to the alias-qualified query.
  - *Solaris (1972 per Wikipedia)*: a year-qualified search for "Solaris 1972"
    returned zero results outright — 1337x tags every release for this film
    **1971** (shoot year, not release year). A same-session fallback search
    without a year ("Solaris Tarkovsky") still missed the good release,
    surfacing only two old 3-4-seeder uploads, because the fallback wasn't
    the plain bare-title-sorted-by-seeders search the fix above calls for.
    The real answer (139 seeders) only appeared under "Solaris 1971".
- **Verified 2026-08-15: an old/thin-swarm release can look alive at add time
  and still be dead.** A release with "5 seeders" at Prowlarr search time
  (North Face/Nordwand 2008, 11.8GB) went to `stalledDL` with 0 seeders within
  under a minute of being added to qBittorrent — seeder counts from search are
  a snapshot, not a live guarantee, especially on older/rarer titles. If a
  torrent is still `metaDL`/`stalledDL` with 0 seeders after ~30-60s, don't
  keep waiting indefinitely — cancel (`download_cancel`, leaves no files since
  nothing downloaded yet) and try the next-best release rather than trusting
  the original seeder count.
- **Indexer-to-content-type routing is fixed by convention, not by Prowlarr's
  own category tagging: `1337x` = movies, `BroadcasTheNet` = TV shows.** Don't
  search both at once for either content type — always pass an explicit
  `indexer=` matching the content type. This is deliberate: it sidesteps the
  private-tracker-timeout risk below entirely (no reason to mix them in one
  call), and BroadcasTheNet's catalog is TV-only anyway per `list_indexers`
  (`"carries": ["TV"]`), so a movie search against it is pointless.
  **This skill is movie-only** (see When to use) — a TV acquisition flow would
  need its own skill built around `indexer="BroadcasTheNet"` and season-pack
  logic; don't improvise season-pack handling here.
- **Verified 2026-08-16: `mcp__prowlarr__search` does NOT silently truncate
  before ranking.** A live test (single indexer, well-known title) returned
  `total_before_ranking: 49` while `limit=10` — the tool pools the *entire*
  per-indexer result set, ranks over all of it, then truncates to `limit`. So
  a returned "best torrent" really is best-of-everything-fetched, not
  best-of-an-arbitrary-slice. No pagination/offset param exists on the tool,
  but none is needed given this behavior — there's nothing sitting past the
  fetch window to page into.
- **A multi-indexer search that includes a private tracker (BroadcasTheNet)
  can time out at 300s and return nothing, rather than a partial/wrong
  answer.** Verified: a 2-indexer search (1337x + BroadcasTheNet, `limit=50`)
  hit the full 300s MCP timeout with no result, while the same query against
  1337x alone returned in seconds. Per the tool's own docs, private-tracker
  challenges are solved inside Prowlarr and are "working as intended" slow.
  Moot for this skill given the fixed movies→1337x routing above, but relevant
  if a future TV-acquisition flow ever needs to check both.
- Prowlarr result titles are inconsistent about "1995" vs "Se7en" vs "Seven"
  — search the way IMDb spells it, then retry with the common alt-spelling if
  zero results.
- **Verified 2026-08-16, batch of 10: the `year=` parameter on
  `mcp__prowlarr__search` returned zero results for at least 2 of 10 legitimate
  titles** ("Halloween III Season of the Witch", year=1982; "Slaughterhouse",
  year=1986) even though bare-title-only searches for the same films returned
  healthy results seconds later. Don't trust a zero-result year-qualified
  search as proof of absence — always retry with the year param dropped
  entirely (not just reworded) before concluding nothing exists.
- **Verified 2026-08-16: a bare title alone can silently return the WRONG
  film when a same-named or similarly-named title is more popular.** Searching
  bare "Slaughterhouse" (intending the 1986 Australian slasher) returned only
  *Slaughterhouse Rulez (2018)*, an unrelated British comedy, because it had
  440 seeders vs. the real target's low count — the tool has no way to know
  which "Slaughterhouse" you meant and just ranks by seeders. Sanity-check the
  returned title/year against what you actually intended before downloading;
  if the top hit's year or vibe doesn't match, add a *bare* qualifying word
  (not the `year=` field, which can zero out — see above) like the actual
  release year as a plain string in the query, e.g. "Slaughterhouse 1987".
- `mcp__qbt__download`'s `category` and `name` params are frequently silently
  ignored in favor of auto-detected routing — don't treat that as a failure,
  just verify `save_path` in the response.
- Don't quote seeder/size numbers from a stale search — Prowlarr results are
  live at call time; if minutes pass before downloading, they may have shifted
  slightly (rarely enough to matter, but don't cache and reuse a search result
  from an earlier session).

## Verification

- Absence confirmed via `mcp__plex__search` (2-3 phrasings, all misses).
- Release chosen has a stated reason (seeders/size/codec tradeoff), not just
  "the first result."
- `mcp__qbt__download` response has `confirmed: true`.
- Final `mcp__qbt__downloads` check shows `state: downloading` with nonzero
  speed and seeders — not just "added successfully."
