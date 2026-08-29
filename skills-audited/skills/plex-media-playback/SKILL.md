---
name: plex-media-playback
description: Play movies and TV on the house Plex players, control playback, recommend things to watch, and answer questions about the library as a whole. Use whenever someone wants something put on a TV, wants to know what is playing, wants it stopped or paused, asks for something to watch ("a fantasy epic", "something like Inception", "what's new"), or asks what the collection is missing ("do we have any Kubrick", "what classics am I missing", "which shows have gaps"). Everything goes through the `plex` MCP server.
tags: []
related_skills: []
---

<!-- NOTE: duplicates skills-from-hermes/media/plex-media-playback — see SKILL_AUDIT_RESULTS.md conflicts section; one of the two should be retired -->

# Plex Media Playback

All Plex work goes through the **`plex` MCP server**. It is the only supported
path. Call its tools directly.

Do not write Python or `curl` against the Plex API, do not call Home Assistant,
and do not construct playback requests by hand. Those approaches were tested
against this exact hardware and they fail — see
[references/troubleshooting.md](references/troubleshooting.md) for what was
tried and the errors each returned. If a tool fails, the fix is never a
different transport.

**Home Assistant is not a media path.** It handles lights, blinds, thermostats
and scenes through the `hass` server, and that is all it is for. Its Plex
integration hits the same limits this server does and adds a layer of
indirection on top. Never reach for it here.

## Tools

| Need | Tool |
| --- | --- |
| Is anything wrong? | `plex_status` |
| What can I play to? | `list_players` |
| Play a specific title | `play` |
| Play an exact item after disambiguating | `play_rating_key` |
| Next episode of a show | `play_next_episode` |
| Pause / resume / stop / skip / shuffle | `control` |
| Jump to a position, or skip ahead/back | `seek` |
| Subtitles on/off, audio track | `set_streams` |
| Change volume | `set_volume` |
| What's on right now | `now_playing` |
| Find a title | `search` |
| What genres exist | `library_overview` |
| Open-ended suggestions | `discover` |
| "Something like X" | `similar_to` |
| Continue watching | `on_deck` |
| Recently added | `recently_added` |
| What have I been watching | `watch_history` |
| What libraries exist | `list_libraries` |
| Playlists | `list_playlists`, `play_playlist`, `create_playlist` |
| **Everything in the library** | `library_export` |
| **Shape of the collection** | `library_stats` |
| **Do we have these titles?** | `check_titles` |
| **Missing episodes, low-res files, bad metadata** | `find_gaps` |
| Scan for newly added files | `refresh_library` |
| Fix one item's metadata | `refresh_item` |
| Repair watch state | `mark_watched` |

## Playing something

1. Call `play` with the title and the player. That is usually the whole job —
   `play` searches, picks the best match, wakes the device if its Plex app is
   closed, starts playback, and confirms a session actually began.
2. Check `confirmed_playing` in the response. If it is `false`, playback did
   **not** start; read `playback_state` and report that. Do not claim something
   is playing because the call returned `ok: true`.
3. If `other_matches` looks like the user may have meant a different title, say
   so. Do not silently play the wrong thing.

Player names: say the room. `list_players` gives each device a `room` when one
is configured, and that is what to pass and what to say back — "playing in the
theater", not "on Streaming Stick 4K". Wording is forgiving within a configured
room ("the lounge", "front room"), but a room that is not in the map is not a
room; anything listed under `unmapped` has no room yet, so use its `name`
verbatim. Never invent or abbreviate either.

Asking for a **show** plays the next unwatched episode, not a menu. That is
intended.

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

Recommend from what `discover` returned. **Never suggest a title you have not
seen in a tool result** — a plausible-sounding film that is not on the server
wastes the user's time. `discover` is paged: if `next_offset` comes back, there
are more results than you asked for.

If nothing matches, say the library has nothing matching and offer to widen the
filters. Do not invent titles to fill the gap.

## Whole-library questions

"What am I missing", "do we have much sci-fi", "what should I upgrade" are
questions about the collection, not about one title. They have their own tools
and the wrong approach is expensive:

**Never enumerate a library by calling `discover` over and over.** Slicing it
by year or genre to walk the whole collection burns the entire budget and still
misses things. `library_export` returns *every* title in one call — the whole
movie library is a few thousand tokens at `detail=minimal`.

The order that works:

1. `library_stats` first. It gives counts by decade, genre, resolution and
   watched state without listing a single title, and thin buckets are the
   clearest gap signal there is.
2. `library_export detail=minimal` when you need the actual titles. Its
   response says `complete: true` when you have all of them — at that point
   **anything not in the list is not on the server**, and you can say so
   flatly.
3. `check_titles` to test a hypothesis. Propose the classics, the franchise
   entries, the director's filmography — pass them all in **one** call, one per
   line. Do not run a `search` per title.
4. `find_gaps` for holes that need no outside knowledge: TV seasons with
   episodes missing, movies still at 720p or below, items Plex failed to match.

Reading `check_titles`: `missing` is authoritative — say those are absent.
`uncertain` is not a hit; it means the closest thing on the server has a
different year or a slightly different title, so name what was found and ask,
rather than reporting either "you have it" or "you don't".

`find_gaps` infers missing episodes from the numbers present, so a show that
numbers episodes absolutely rather than per-season can look broken. Check
`highest_present` against the real season length before telling someone to go
download something.

## Keeping the library current

If someone says they just added files and Plex does not show them, run
`refresh_library`. Nothing else will make new files appear, and until it runs
every tool here will correctly report them as missing.

`refresh_metadata=true` re-pulls metadata for the entire library and can run
for hours. Do not set it to fix one bad poster — that is `refresh_item`.

## Stopping and controlling

`control` handles play, pause, stop, next, previous.

`stop` works on **every** device that is streaming, including ones that reject
pause and seek. If someone asks to stop a TV, just do it — do not check
capabilities first.

Pause, seek and volume need the device to support remote control. When they
fail, the error says so specifically.

"Skip ahead a bit" is `seek delta_seconds=...`, not `seek seconds=...` — the
latter jumps to an absolute position from the start, which is almost never what
was meant. Negative values rewind.

"Turn on subtitles" is `set_streams`. It needs something playing, because
subtitle and audio track ids belong to the item in the current session. If the
item has no subtitle tracks at all, the error says that — report it rather than
retrying.

## Device status — read it before acting

`list_players` reports `controllable` and a `status` for each device. The
outcomes and what to do:

- **ready** — go ahead.
- **"registered but not listening"** — the Plex app is closed. `play` handles
  this automatically for Rokus by launching Plex first; it takes ~15 seconds.
  If waking fails, the device is powered off. Say that and stop.
- **"never advertises itself as a player"** — that device can **never** be a
  playback target. This is final. Report it and stop.
- **"streaming now, but not registered as a controllable client on this
  account"** — the device is signed in as a different Plex user. You can see
  and name what it is playing; you cannot drive it. Report that and stop.

That last case is permanent — see
[references/troubleshooting.md](references/troubleshooting.md) for why. You can
still `stop` it.

## Hard rules

- Report tool errors **verbatim**. The error text names the real cause.
- Never retry a failed call with altered arguments hoping for a different
  result. If a call fails, read why.
- Never fall back to Home Assistant, raw HTTP, or a hand-written script.
- Never claim something is playing without `confirmed_playing: true`.
- If a device the user expects is missing, report that rather than substituting
  a different room. Playing a movie on the wrong TV is worse than not playing it.
