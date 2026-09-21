---
name: plex-media-playback
description: Play movies and TV on the house Plex players, control playback, recommend things to watch, and answer questions about the library as a whole. Use whenever someone wants something put on a TV, wants to know what is playing, wants it stopped or paused, asks for something to watch ("a fantasy epic", "something like Inception", "what's new"), or asks what the collection is missing ("do we have any Kubrick", "what classics am I missing", "which shows have gaps"). Everything goes through the `plex` MCP server.
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
| Re-pull one item's metadata from the provider | `refresh_item` |
| Repair watch state | `mark_watched` |
| **Read one item's editable metadata** | `get_item_metadata` |
| **Correct a title, year or genres; apply a label** | `update_item_metadata` |
| **A reviewed correction pass, up to 25 items** | `batch_update_item_metadata` |
| **Find items that need fixing** | `audit_library` |
| **What does Plex think this file is?** | `list_match_candidates` |
| **Re-bind a wrongly matched file** | `fix_match` |
| **Posters and background art** | `get_artwork`, `set_artwork` |
| **Alternate / textless / fan-made art** | `find_alternate_art` |
| Undo a correction's field lock | `unlock_metadata_fields` |
| **Write the audit up for a human** | `write_review_document` |

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

## Repairing a broken item

A movie showing as a black rectangle with a title like `The Entity Horror` is
**not** a missing-poster problem and **not** three wrong fields. It is a file
Plex never matched: the title is whatever the file was called, and there is no
poster because there is no provider entry to get one from.

Editing the title and adding a genre gives you a correctly labelled record that
still has no summary, no cast, no ratings and no artwork. Rematching gives you
all of it at once.

**The order is: match, look, correct what is left, lock.**

1. `audit_library` — which items are wrong, and why.
2. `list_match_candidates` — what does Plex think this file is? Pass an
   explicit `title=` and `year=` for what *you* believe it is; searching under
   the mangled title usually returns nothing useful.
3. `fix_match` with `confirm=true` — restores title, year, summary, genres and
   artwork together.
4. `get_item_metadata` — see what actually landed.
5. `update_item_metadata` — only the residue a provider cannot know: a
   deliberate label, a genre the provider gets wrong.

Never the other way round. `update_item_metadata` locks every field it writes,
and a locked field is exactly what a rematch cannot replace, so correcting
first turns into a rematch that looks like it half worked. If that has already
happened, `unlock_metadata_fields` undoes it; if you know it will,
`fix_match unlock_first=true`.

`fix_match` replaces the entire record. That is the right repair for an
unmatched file and a destructive one for something a human already corrected by
hand. Check `locked_fields` before running it — locks are usually the sign that
somebody made a deliberate choice you are about to overwrite. Ask.

### Identifying the film is your job

`audit_library` reports *that* a title looks wrong and *why* — a genre word on
the end, a stranded foreign article, two titles welded together. It never says
what the film actually is, because that needs knowing which films exist.

Work from the `file` field on each finding. On a broken item the filename is
usually the only honest identifier left. Then confirm your guess through
`list_match_candidates` rather than asserting it — if Plex's agent offers *The
Entity (1982)* for that file, you were right.

If you cannot identify something confidently, say so and leave it in the review
document as unresolved. A confident wrong correction is worse than a gap,
because it gets locked and nobody looks twice.

### Artwork

`get_artwork` lists what the item's own agent offers; `set_artwork poster_id=`
selects one. Prefer that always.

`poster_url` makes the Plex server download a URL you chose. Use it only when
the agent offers nothing, and tell the user you are doing it.

An item with no poster **and no candidates** is unmatched. Do not upload a
poster onto it — fix the match and the poster arrives on its own.

### Alternate art

`find_alternate_art` reads three sources at once: what Plex's agent already
offers, TMDB (every language, plus textless variants), and fanart.tv
(community-made posters, clearlogos, disc art). It returns candidates and
changes nothing.

- Apply a `plex` candidate with `set_artwork poster_id=<id>`.
- Apply a `tmdb` or `fanart` one with `set_artwork poster_url=<url>`.
- `kind` is `poster`, `background` or `logo`.
- `language=textless` finds art with no title burned into it, which is what you
  want wherever Plex draws the title itself.

Read the `unavailable` block before telling anyone there is no alternate art —
an unset API key looks exactly like an empty result if you do not.

Only hosts on the server's allowlist can be downloaded from, and it defaults to
the two APIs above. If a URL is refused, that is configuration and not
something to route around: report it and move on.

### The review pass

For anything beyond one or two items, do not apply as you go:

1. `audit_library` to find the work.
2. Identify each film and decide the fix.
3. `write_review_document` with your proposals and the exact
   `batch_update_item_metadata` payload.
4. **Show the user the path and wait.**
5. Apply only what they approve.

This is the whole point of the tooling. Every write tool has a dry run and
refuses to act without `confirm=true` so that this pass is the easy path.

### Labels a filter depends on

When something downstream filters on a label rather than a genre, find the gaps
in one call:

`audit_library checks=["labels"] require_label="Horror Marathon" when_genre="Horror"`

Keep the two jobs separate. `Horror` the genre means the film is horror.
`Horror Marathon` the label means somebody chose to programme it. Adding the
label must not touch the genres.

## Correcting metadata

`refresh_item` asks the provider again and takes whatever comes back. Use it
when the item is matched correctly and the data is merely stale. When the match
itself is wrong — an Italian release title sitting where the English one
should be, a year off by a decade, a horror film filed as Drama — asking again
returns the same wrong answer. That is what `update_item_metadata` is for.

Never run `refresh_item` after a correction. It can overwrite a deliberate fix
with the provider data that was wrong in the first place. Refresh first if at
all, then correct.

**The workflow is always the same three steps.** Do not collapse it.

1. `get_item_metadata` — read the current state. You need the existing genre
   list before you can write a correct one.
2. Propose the change with `dry_run: true` and **show the diff to the user**.
3. Resend with `confirm: true` once they have agreed.

**Arrays replace.** `genres: ["Horror", "Mystery"]` is the complete intended
list, not an addition. Send every genre the item should end up with, including
the ones already there. An empty array is refused outright — erasing takes
`clear_genres`, `clear_labels`, `clear_collections` or `clear_countries`.

**A write needs a `rating_key`.** `get_item_metadata query="..."` will find one,
and refuses to choose when the title is ambiguous. Report the candidates and
ask which one; never pick. Add the year to settle it: `query="Black Sunday
(1960)"`.

**Check `verified` on every result.** A write that came back `ok: true` with
`verified: true` landed. Anything else did not, whatever the HTTP status was.
On `readback_mismatch`, report the `after` block — it is what is actually on
the server now — and do not retry blindly.

Corrected fields are locked so a later refresh cannot undo them. That is
deliberate; do not unlock them.

### Genre or label?

Two different jobs. Mixing them pollutes every future "show me a horror film"
query:

- **Genre** — the film genuinely *is* horror and Plex has it missing or wrong.
  `genres: ["Horror", "Thriller", "Drama"]`
- **Label** — deliberate programming. A horror-adjacent title that belongs on
  the marathon list but should not be filed as Horror.
  `labels: ["Horror Marathon"]`

Adding the marathon label does not touch the genres, and it must not. Replace
the genre list only when the correction supplies the full intended list.

### More than one item

`batch_update_item_metadata` takes up to 25, validates every target before
writing any of them, and verifies each one separately. Same workflow: dry run,
human reads the diff, confirmed run. A batch that comes back with entries in
`failed` is partly done — report which rating keys those were rather than
re-running the whole pass.

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
- Never claim metadata was corrected without `verified: true`.
- Never upload a poster onto an item that has no match. Fix the match.
- Never guess what a film is in a confirmed write. Confirm the guess
  through `list_match_candidates`, or leave it unresolved in the review.
- Never write metadata without showing the user the dry-run diff first.
- If a device the user expects is missing, report that rather than substituting
  a different room. Playing a movie on the wrong TV is worse than not playing it.
