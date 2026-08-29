# Incident log: search-quality misses

Read this for the evidence behind the "bare title first" and "don't trust a
zero/thin result" rules in `SKILL.md` steps 2 and 2a/3, or when debugging a
fresh miss that looks similar.

## Root cause behind two separate misses in one session (2026-08-15)

A *qualified* search query (a year, a foreign-language alias) was trusted as
exhaustive when it was actually the source of the miss.

- **North Face (2008)**: searching "Nordwand 2008" (the German alias) returned
  only 4 weak/old results (best: 5 seeders, 11.8GB); the actual best release
  was titled plainly "North Face (2008) [...] [YTS] [YIFY]" with 121 seeders
  at 2.3GB — invisible to the alias-qualified query.
- **Solaris (1972 per Wikipedia)**: a year-qualified search for "Solaris 1972"
  returned zero results outright — 1337x tags every release for this film
  **1971** (shoot year, not release year). A same-session fallback search
  without a year ("Solaris Tarkovsky") still missed the good release,
  surfacing only two old 3-4-seeder uploads, because the fallback wasn't the
  plain bare-title-sorted-by-seeders search the fix calls for. The real
  answer (139 seeders) only appeared under "Solaris 1971".

This is the incident behind the "start bare, qualify only to cut noise, drop
the qualifier on a thin result" rule in step 2.

## `year=` parameter unreliable (verified 2026-08-16, batch of 10)

The `year=` parameter on `mcp__prowlarr__search` returned zero results for at
least 2 of 10 legitimate titles ("Halloween III Season of the Witch",
year=1982; "Slaughterhouse", year=1986) even though bare-title-only searches
for the same films returned healthy results seconds later. This is why a
zero-result year-qualified search is never treated as proof of absence.

## Bare title can return the wrong film (verified 2026-08-16)

Searching bare "Slaughterhouse" (intending the 1986 Australian slasher)
returned only *Slaughterhouse Rulez (2018)*, an unrelated British comedy,
because it had 440 seeders vs. the real target's low count — the tool has no
way to know which "Slaughterhouse" you meant and just ranks by seeders. This
is the origin of the "sanity-check the returned title/year" rule, and of
using a bare qualifying word (e.g. "Slaughterhouse 1987") rather than the
`year=` field, which can zero out per the finding above.

## Old/thin-swarm release looked alive at add time, was dead within a minute (verified 2026-08-15)

A release with "5 seeders" at Prowlarr search time (North Face/Nordwand 2008,
11.8GB) went to `stalledDL` with 0 seeders within under a minute of being
added to qBittorrent. Basis for the "check twice before judging health" and
"cancel and try next-best" rules.

## `mcp__prowlarr__search` ranks over the full result set, not a slice (verified 2026-08-16)

A live test (single indexer, well-known title) returned
`total_before_ranking: 49` while `limit=10` — the tool pools the *entire*
per-indexer result set, ranks over all of it, then truncates to `limit`. A
returned "best torrent" really is best-of-everything-fetched. No
pagination/offset param exists on the tool, and none is needed.

## Private-tracker timeout (verified)

A multi-indexer search that includes a private tracker (BroadcasTheNet) can
time out at 300s and return nothing, rather than a partial/wrong answer.
Verified: a 2-indexer search (1337x + BroadcasTheNet, `limit=50`) hit the full
300s MCP timeout with no result, while the same query against 1337x alone
returned in seconds. Per the tool's own docs, private-tracker challenges are
solved inside Prowlarr and are "working as intended" slow. Moot for this
skill given the fixed movies→1337x routing, but relevant if a future
TV-acquisition flow ever needs to check both.
