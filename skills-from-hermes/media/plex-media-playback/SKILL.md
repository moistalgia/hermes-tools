---
name: plex-media-playback
description: Play movies and TV on the house Plex players, control playback, and recommend things to watch from the library. Use whenever someone wants something put on a TV, wants to know what is playing, wants it stopped or paused, or asks for something to watch ("a fantasy epic", "something like Inception", "what's new"). Everything goes through the `plex` MCP server.
tags: []
related_skills: []
---

# Plex Media Playback

All Plex work goes through the **`plex` MCP server**, whose tools are exposed as
`mcp__plex__<tool>` (double underscores). It is the only supported path.

Do not write Python or `curl` against the Plex API, do not call Home Assistant,
and do not construct playback requests by hand. Those approaches were tested
against this exact hardware and they fail — see
[references/troubleshooting.md](references/troubleshooting.md) for what was
tried and the errors each returned. If a tool fails, the fix is never a
different transport.

**Home Assistant is for blinds. Nothing else.** Its Plex integration hits the
same limits this server does and adds a layer of indirection on top. Never
reach for it here.

For acquisition-side work — auditing the library, finding gaps, proposing titles
to **add** that aren't on the server — use the `plex-library-curator` skill
instead. This skill only recommends from what's already there.

## Config prerequisite — check readiness before starting

The Plex tools exist only if the `mcp_servers: plex:` entry is loaded from
`~/.hermes/config.yaml`, which Hermes reads **at startup**. The MCP server runs
host-side on Windows, not in the Docker container.

If `mcp__plex__*` tools don't resolve, do NOT try another transport — there is
no in-container fallback. See
[references/mcp-readiness-guide.md](references/mcp-readiness-guide.md) for the
diagnostic sequence.

## Tools

All names below take the `mcp__plex__` prefix. As of 2026-08-15 the Plex MCP
exposes **28 tools** — the earlier "only 15 exist, list_libraries/
list_playlists/play_playlist do not exist" note was wrong for this snapshot;
those tools (and 10 more) are enabled now. Corrected below.

Playback and discovery (this skill's core):

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

Playlists:

| Need | Tool |
| --- | --- |
| List playlists on the server | `list_playlists` |
| Build one from specific items (movie night, themed run) | `create_playlist` |
| Play an existing playlist on a player | `play_playlist` |

Library maintenance (fixing state, not playing anything):

| Need | Tool |
| --- | --- |
| Fix a wrong watched/unwatched flag | `mark_watched` |
| Re-download metadata for one item (wrong poster, mismatched episode) | `refresh_item` |
| Scan a library for new files after an add | `refresh_library` |

Census / gap-analysis tools (`library_stats`, `check_titles`, `library_export`,
`find_gaps`) exist too but are curation-side — see `plex-library-curator`,
which now uses them directly instead of looping `discover`.

Use `library_overview` (or the newer `list_libraries` for a bare list) for
library structure.

## Playing something

1. Call `play` with the title and the player. That is usually the whole job —
   `play` searches, picks the best match, wakes the device if its Plex app is
   closed, starts playback, and confirms a session actually began.
2. Check `confirmed_playing` in the response. If it is `false`, playback did
   **not** start; read `playback_state` and report that. Do not claim something
   is playing because the call returned `ok: true`.
3. **`offset_seconds` is unreliable when the title has an existing Plex resume
   point** (watched/in-progress elsewhere, shared across users on this
   server). `play(..., offset_seconds=0)` can silently keep the old resume
   position instead of restarting from zero — verified 2026-08-16 replaying
   The Matrix. If the reported `playback_state` position doesn't match the
   `offset_seconds` you asked for, don't retry `play` — call `seek(player=...,
   seconds=<target>)` directly afterward; that reliably corrects position.
4. If `other_matches` looks like the user may have meant a different title, say
   so. Do not silently play the wrong thing.

Player names: room names like "theater" or "office" work **only if the host's
`PLEX_ALIASES` env var is configured** — this maps a room name to a device
`machine_identifier` (see "Room aliases" below). If a name does not resolve,
`list_players` returns the real ones — use them verbatim. Never invent or
abbreviate a player name.

### Room aliases (`PLEX_ALIASES`)

The Plex MCP supports a `PLEX_ALIASES` env var (JSON map of room → device) so
you can say "the theater" instead of "Streaming Stick 4K". When configured,
`list_players` echoes a `room` field per device, and anything without one
falls into `unmapped`. **As of 2026-08-16 this IS wired up** — `list_players`
returns real room strings: Sleepy → `living room`, Andies Tv For Ants →
`andies office`, Roku Express 4K+ → `bedroom`, unknown/Fire TV →
`nicks office`, Streaming Stick 4K → `theater`. Only DESKTOP-CHB1M9E has no
room mapped (`room: null`, appears in `unmapped`). Address any of the mapped
five by room name; use DESKTOP-CHB1M9E's literal Plex name.

Known device identifiers, for whenever aliasing gets configured (map key is
the room, first array element must be the `machine_identifier`, not the
display name — display names are user-settable and unstable):

| Plex name | machine_identifier | Notes |
| --- | --- | --- |
| Sleepy | `a710a60ff65de04711dd2c4f217fada3` | Roku, currently the only reliably controllable player |
| Andies Tv For Ants | `9fa5ef017bd8b903395f9e479aa9bd91` | Roku |
| DESKTOP-CHB1M9E | `t0v7x03y0qggo77gd92xd2t9` | Plex Media Player (Konvergo) |
| Roku Express 4K+ | `95c030af1faf5801835d4601a8b37004` | Roku |
| Streaming Stick 4K | `d2b46d2ad54416315e5e36862d2644a1` | Roku |
| unknown | `gd91wa2zwieprb2mbmd1r0u3` | Amazon Fire TV — never controllable, see below |

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
seen in a tool result** — this is a small library (a few hundred movies, a few
dozen shows; call `library_overview` for live counts), and a plausible-sounding
film that is not on the server wastes the user's time.

If nothing matches, say the library has nothing matching and offer to widen the
filters. Do not invent titles to fill the gap.

## Stopping and controlling

`control` handles play, pause, stop, next, previous.

`stop` works on **every** device that is streaming, including ones that reject
pause and seek. If someone asks to stop a TV, just do it — do not check
capabilities first.

Pause, seek and volume need the device to support remote control. When they
fail, the error says so specifically.

## Device status — read it before acting

`list_players` reports `controllable` and a `status` for each device. The three
outcomes and what to do:

- **ready** — go ahead.
- **"registered but not listening"** — the Plex app is closed. `play` handles
  this automatically for Rokus by launching Plex first; it takes ~15 seconds.
  If waking fails, the device is powered off. Say that and stop.
- **"never advertises itself as a player"** — that device can **never** be a
  playback target. This is final. Report it and stop.

That last case is not a transient error and not a configuration problem. There
is no argument variation, alternate endpoint, or integration that changes it.
The office Fire TV is in this category: it will stream happily and refuse every
command, so an active session on it is **not** evidence that control will work.
You can still `stop` it.

This was verified directly against a live Fire TV session, mid-playback:
`/clients` came back empty, `protocolCapabilities` on the session was `['']`,
a relayed read-only `timeline/poll` returned 404, and even Home Assistant's own
Plex integration refused with "Client is not currently accepting playback
controls." The only remote-control path to a Fire TV at all is ADB (Home
Assistant's `androidtv` integration), which is out of scope here — hardware fix
pending on Moisty's end, not a software one.

`control action="stop"` works on it anyway because `stop` is a **server-side**
call (`/status/sessions/terminate`) that never touches the device's Companion
listener — that's why it's the one command that reaches every streaming
device regardless of `controllable`. Pause, seek, and volume all require
Companion and stay unavailable on Fire TV.

## Moving playback between players

When a title is already playing on device A and the user asks for it elsewhere:

1. Call `now_playing` — find running session and source player.
2. If active, call `control(action="stop")` (or `pause()`) **on the source first**. Only then does `play(query=..., player=<dest>)` succeed. Even with an explicit `player=` name, if a session is in-flight the MCP tool may lock to that session as default and refuse: "No player specified and more than one is available."
3. Call play at destination player. If desired position matters, use `seek`.
4. Verify continuation by calling `now_playing` again — confirm source stopped and destination playing.

If nothing was already playing, call `play(query=..., player=<dest>)` directly.

## Playlists

- `list_playlists` — see what's already built.
- `create_playlist` — assemble a specific set of items (movie night line-up, a
  themed run of episodes) from search/discover results. Give it explicit
  items; don't ask it to guess a theme.
- `play_playlist` — plays it on a named player, same player-resolution rules as
  `play` (use `list_players` names verbatim).

## Fixing library state (not playback)

These don't play anything — they correct what Plex thinks is true:

- `mark_watched` — repair watch state (watched elsewhere, or Plex marked an
  episode played by accident). Confirm the item first with `search`.
- `refresh_item` — re-pull metadata for one item: wrong poster, missing
  summary, an episode matched to the wrong show.
- `refresh_library` — rescan a whole library section. Run this after new files
  land (e.g. after `plex-acquire-movie` finishes a download) — until it runs,
  the new file is invisible to every other tool here, correctly.

## Hard rules

- Report tool errors **verbatim**. The error text names the real cause.
- Never retry a failed call with altered arguments hoping for a different
  result. If a call fails, read why.
- Never fall back to Home Assistant, raw HTTP, or a hand-written script.
- Never claim something is playing without `confirmed_playing: true`.
- If a device the user expects is missing, report that rather than substituting
  a different room. Playing a movie on the wrong TV is worse than not playing it.
