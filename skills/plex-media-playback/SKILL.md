---
name: plex-media-playback
description: Play movies and TV on the house Plex players, control playback, and recommend things to watch from the library. Use whenever someone wants something put on a TV, wants to know what is playing, wants it stopped or paused, or asks for something to watch ("a fantasy epic", "something like Inception", "what's new"). Everything goes through the `plex` MCP server.
tags: []
related_skills: []
---

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
| Pause / resume / stop / skip | `control` |
| Jump to a position | `seek` |
| Change volume | `set_volume` |
| What's on right now | `now_playing` |
| Find a title | `search` |
| What genres exist | `library_overview` |
| Open-ended suggestions | `discover` |
| "Something like X" | `similar_to` |
| Continue watching | `on_deck` |
| Recently added | `recently_added` |
| What libraries exist | `list_libraries` |
| Playlists | `list_playlists`, `play_playlist` |

## Playing something

1. Call `play` with the title and the player. That is usually the whole job —
   `play` searches, picks the best match, wakes the device if its Plex app is
   closed, starts playback, and confirms a session actually began.
2. Check `confirmed_playing` in the response. If it is `false`, playback did
   **not** start; read `playback_state` and report that. Do not claim something
   is playing because the call returned `ok: true`.
3. If `other_matches` looks like the user may have meant a different title, say
   so. Do not silently play the wrong thing.

Player names: room names like "theater" or "office" work if configured. If a
name does not resolve, `list_players` returns the real ones — use them verbatim.
Never invent or abbreviate a player name.

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
seen in a tool result** — the library has 491 movies and 48 shows, and a
plausible-sounding film that is not on the server wastes the user's time.

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

## Hard rules

- Report tool errors **verbatim**. The error text names the real cause.
- Never retry a failed call with altered arguments hoping for a different
  result. If a call fails, read why.
- Never fall back to Home Assistant, raw HTTP, or a hand-written script.
- Never claim something is playing without `confirmed_playing: true`.
- If a device the user expects is missing, report that rather than substituting
  a different room. Playing a movie on the wrong TV is worse than not playing it.
