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

Three host-side MCP servers: `mcp__plex__*`, `mcp__prowlarr__*`,
`mcp__qbt__*`. If a tool name doesn't resolve, don't hand-roll a curl/API
workaround — report it and stop. Run `qbt_status` / `prowlarr_status` /
`plex_status` first if anything seems off.

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
   sort="seeders")`.
   - **Start bare.** Bare-title, seeder-sorted results surface the
     best release regardless of what year or alias the indexer filed it
     under — it's the query with the fewest assumptions baked in.
   - **Add a year only to cut noise**, if the bare search is swamped with
     unrelated junk (common single-word titles pull in shorts or unrelated
     adult content; add the year at that point purely as a filter). Never let
     the year-qualified search be the *only* one you run.
   - **If a qualified search comes back thin or empty, that means "drop the
     qualifier," not "try a different qualifier."** Re-run the plain
     bare-title search sorted by seeders before reaching for a second guess
     (alternate spelling, director's name).
   - **Sanity-check seeder count against the title's fame.** A canon/notable
     film returning only single-digit seeders on your first search is itself
     a signal something was missed — recheck with a bare search before
     accepting it.
   - Always scope to `indexer="1337x"` explicitly for movies — BroadcasTheNet
     is TV-only (see Pitfalls). Returns pre-parsed results with resolution,
     codec, size, seeders, age, and a ready `magnet` field. See
     [references/incident-log.md](references/incident-log.md) for the
     incidents behind this rule.

2a. **Batch acquisition (multiple titles in one run) — control context, don't
    let it control you.** Fix, in order of impact:
    - **Cap `limit` to 5 per search, always.** You only need the top handful
      to make a seeders-vs-resolution call — verified across a 10-title batch
      with every pick still correct.
    - **Always pass `indexer="1337x"` explicitly** (see Pitfalls) — halves the
      raw payload per call by skipping multi-indexer merge/dedup and avoids
      the private-tracker timeout risk.
    - **Fire independent title searches in parallel, one batch of tool calls
      per turn.** Ten titles that don't depend on each other should be ten
      `mcp__prowlarr__search` calls issued together, followed immediately by
      the matching `mcp__qbt__download` calls for whichever came back clean,
      also batched.
    - **Quote only the fields that matter in your summary** (title,
      resolution, seeders, size) — never restate the full magnet/tracker
      block; pass `magnet` straight from tool output to `mcp__qbt__download`
      without echoing it.
    - **Don't re-run a search "just to double check" once you have a clean
      hit** — Prowlarr results are ranked and live; re-querying without new
      information just duplicates payload.
    - **Budget one retry per title, not a search spiral.** One corrective
      re-query (bare + a plain-string year) is enough for wrong-film noise or
      an empty year-qualified search; if that also fails, stop and flag the
      title rather than trying five spelling variations.

3. **Pick a release: highest seeders wins, 1080p, no 720p, size is a
   secondary signal not a hard filter.**
   - **Default policy, per moisty: take the highest-seeder 1080p release,
     full stop — don't downgrade to 720p to chase a smaller file size.** Size
     (he aims for 0.5-4GB; smaller often means more seeders anyway) is a
     heuristic for guessing which release might be well-seeded, not a filter
     that overrides seeder count once you can see the real numbers. A
     highest-seeder pick outside 0.5-4GB is worth a one-line flag in your
     summary, not a disqualifier.
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
   **Check twice, a few seconds apart, before judging health.** The first
   `downloads` read can show `state: downloading` with `seeders: 1,
   speed_mbps: 0.0` and an absurd `eta_minutes` — that's still connecting to
   peers, not a dead swarm. Don't swap releases on that first snapshot;
   re-check once more first.

## Pitfalls

- **An old/thin-swarm release can look alive at add time and still be dead.**
  Seeder counts from search are a snapshot, not a live guarantee. If a torrent
  is still `metaDL`/`stalledDL` with 0 seeders after ~30-60s, cancel
  (`download_cancel`, no files lost) and try the next-best release rather
  than trusting the original seeder count.
- **Indexer-to-content-type routing is fixed by convention: `1337x` = movies,
  `BroadcasTheNet` = TV.** Always pass an explicit `indexer=` matching the
  content type — never search both at once. This skill is movie-only (see
  When to use); a TV flow would need its own skill with season-pack logic.
- **`mcp__prowlarr__search` ranks over the full result set before truncating
  to `limit`** — a returned "best torrent" is best-of-everything-fetched, not
  best-of-a-slice. No pagination param exists or is needed.
- **A multi-indexer search including a private tracker (BroadcasTheNet) can
  hit the full 300s MCP timeout and return nothing**, rather than a
  partial/wrong answer. Moot given the fixed movies→1337x routing above.
- Prowlarr result titles are inconsistent about "1995" vs "Se7en" vs "Seven"
  — search the way IMDb spells it, then retry with the common alt-spelling if
  zero results.
- `mcp__qbt__download`'s `category` and `name` params are frequently silently
  ignored in favor of auto-detected routing — verify `save_path` instead.
- Don't quote seeder/size numbers from a stale search — they're live at call
  time and may have shifted if minutes pass before downloading. See
  [references/incident-log.md](references/incident-log.md) for incidents and
  verification details behind the rules above.

## Verification

- Absence confirmed via `mcp__plex__search` (2-3 phrasings, all misses).
- Release chosen has a stated reason (seeders/size/codec tradeoff), not just
  "the first result."
- `mcp__qbt__download` response has `confirmed: true`.
- Final `mcp__qbt__downloads` check shows `state: downloading` with nonzero
  speed and seeders — not just "added successfully."
