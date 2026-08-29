---
name: plex-media-playback
description: Play movies and TV on the house Plex players, control playback, and recommend things to watch from the library. Use whenever someone wants something put on a TV, wants to know what is playing, wants it stopped or paused, or asks for something to watch ("a fantasy epic", "something like Inception", "what's new"). Everything goes through the `plex` MCP server.
tags: []
related_skills: []
---

# Plex Media Playback

All Plex work goes through the **`plex` MCP server** (`mcp__plex__<tool>`,
double underscores) — the only supported path. **Home Assistant is for blinds,
nothing else.** Never use Home Assistant, raw `curl`, or a hand-written script
for Plex — all three were tested against this hardware and fail (see
[references/troubleshooting.md](references/troubleshooting.md)). If a tool
fails, the fix is never a different transport.

For acquisition-side work — finding gaps, proposing titles to add that aren't
on the server — use `plex-library-curator` instead; this skill only
recommends from what's already there.

If `mcp__plex__*` tools don't resolve, don't try another transport — the
server is host-side on Windows, loaded from `~/.hermes/config.yaml` at Hermes
startup, with no in-container fallback. See
[references/mcp-readiness-guide.md](references/mcp-readiness-guide.md).

## Tools

All names below take the `mcp__plex__` prefix (28 tools total).

| Need | Tool |
| --- | --- |
| Is anything wrong? | `plex_status` |
| What can I play to? | `list_players` |
| Play a specific title | `play` |
| Play an exact item after disambiguating | `play_rating_key` |
| Next episode of a show | `play_next_episode` |
| Pause / resume / stop / skip | `control` |
| Jump to a position | `seek` |
| Change volume | `set_volume` |
| Toggle subtitles / audio track on what's playing | `set_streams` |
| What's on right now | `now_playing` |
| Find a title | `search` |
| What libraries exist (bare list) | `list_libraries` |
| What genres/libraries exist (with vocab) | `library_overview` |
| Open-ended suggestions | `discover` |
| "Something like X" | `similar_to` |
| Continue watching | `on_deck` |
| Recently added | `recently_added` |
| What was actually watched (ground recs in reality, not the watched flag) | `watch_history` |
| List playlists on the server | `list_playlists` |
| Build one from specific items | `create_playlist` |
| Play an existing playlist on a player | `play_playlist` |
| Fix a wrong watched/unwatched flag | `mark_watched` |
| Re-download metadata for one item (wrong poster, mismatched episode) | `refresh_item` |
| Scan a library for new files after an add | `refresh_library` |

Curation tools (`library_stats`, `check_titles`, `library_export`,
`find_gaps`) belong to `plex-library-curator`, not this skill.

## Playing something

1. Call `play` with the title and the player — it searches, picks the best
   match, wakes the device if closed, starts playback, and confirms a session
   began. Usually the whole job.
2. Check `confirmed_playing`. If `false`, playback did **not** start — read
   `playback_state` and report that; don't claim it's playing just because
   `ok: true`.
3. **`offset_seconds=0` can silently keep an old resume point** instead of
   restarting — if reported position doesn't match, call `seek(player=...,
   seconds=<target>)` afterward rather than retrying `play`.
4. If `other_matches` suggests a different title was meant, say so — don't
   silently play the wrong thing.

Room names ("theater", "office") only resolve if `PLEX_ALIASES` is configured
— see [references/room-aliases.md](references/room-aliases.md). Otherwise use
`list_players` names verbatim; never invent or abbreviate one. A **show**
request plays the next unwatched episode, not a menu — that's intended.

## Recommending something

For open requests like "I want a fantasy epic":

1. Call `library_overview` first to get the exact genre vocabulary. Do not guess
   genre names — a wrong one returns an error or an empty set.
2. Call `discover` with the genres that match the request. Multiple genres are
   ANDed by default, which is what you want: a fantasy epic is `Fantasy,Adventure`.
   Set `match_all=false` to widen if nothing comes back.
3. Useful modifiers: `unwatched_only=true` for something new, `min_rating`,
   `decade`, `sort=random` when the user wants to be surprised.
4. For "something like X", use `similar_to` — it ranks by shared genres.

Recommend only from what `discover` returned — never suggest a title you have
not seen in a tool result (small library; a plausible-sounding film that isn't
on the server wastes the user's time). If nothing matches, say so and offer to
widen the filters rather than inventing a title.

## Controlling and device status

`control` handles play, pause, stop, next, previous. `list_players` reports
`controllable` and a `status` per device — check it before acting on anything
but `stop`:

- **ready** — go ahead.
- **"registered but not listening"** — Plex app is closed; `play` auto-launches
  it on Rokus (~15s). If waking fails, the device is powered off — say so and
  stop.
- **"never advertises itself as a player"** — permanently unusable, regardless
  of arguments or transport. Report it and stop. The office Fire TV is in this
  category: an active stream on it is not evidence that control will work. See
  [references/troubleshooting.md](references/troubleshooting.md) for why.

`stop` is the one exception: it's a server-side call
(`/status/sessions/terminate`) that reaches **every** streaming device
regardless of `controllable`, including the Fire TV. If someone asks to stop a
TV, just do it — don't check capabilities first. Pause, seek, and volume need
Companion support and fail with a specific error when it's missing.

## Moving playback between players

If something is already playing elsewhere: call `now_playing` to find the
source, `control(action="stop")` **on the source first** (an in-flight
session can make the MCP tool lock to it and refuse an explicit `player=`
otherwise), then `play(query=..., player=<dest>)` and `seek` if position
matters. Verify with `now_playing` again — confirm source stopped, destination
playing. If nothing was playing, just call `play(query=..., player=<dest>)`
directly.

## Playlists and library state

`create_playlist` needs explicit items from search/discover results — don't
ask it to guess a theme. `play_playlist` uses the same player-resolution rules
as `play`.

`mark_watched`, `refresh_item`, `refresh_library` fix state, they don't play
anything. Confirm the item with `search` before `mark_watched`. Run
`refresh_library` after `plex-acquire-movie` finishes a download — until it
runs, the new file is invisible to every other tool here.

## Hard rules

- Report tool errors **verbatim**. The error text names the real cause.
- Never retry a failed call with altered arguments hoping for a different
  result. If a call fails, read why.
- Never claim something is playing without `confirmed_playing: true`.
- If a device the user expects is missing, report that rather than substituting
  a different room. Playing a movie on the wrong TV is worse than not playing it.
