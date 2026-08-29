# Plex playback: verified failure modes

Read this when a playback tool fails and the reason is not obvious. Everything
below was tested against this house's actual hardware, not inferred from
documentation. Where a claim is marked **dead end**, it has been tried and it
does not work — trying it again will waste the user's time and produce the same
error.

## The one thing to understand

There are three different places Plex reports "players", and they disagree:

| Source | What it contains | Visible while idle |
| --- | --- | --- |
| `/clients` on the media server | devices that registered Companion **with that server** | no |
| `plex.tv/devices.xml` | everything registered to the account, with LAN address | **yes** |
| `/status/sessions` | what is streaming right now | says nothing about control |

A device missing from `/clients` is not offline and not broken. Most streaming
sticks never appear there. `list_players` already merges all three — this table
is here so the failure messages make sense, not because you need to query them.

**Streaming and controllable are independent.** A device can play a movie for
two hours while rejecting every command sent to it. Do not treat an active
session as proof that control will work.

## Failure: "never advertises itself as a player"

The device's plex.tv `provides` field omits `player`. It cannot be a playback
target. **This is permanent, not transient.**

The office Amazon Fire TV is the live example. Tested mid-playback, while it was
actively streaming:

| Attempt | Result |
| --- | --- |
| `/clients` | empty |
| `protocolCapabilities` on its live session | `['']` |
| relayed read-only `timeline/poll` | `404 not_found` |
| relayed `playback/pause` | `404 not_found` |
| Home Assistant `media_player.play_media` | `HomeAssistantError: Client is not currently accepting playback controls` |
| `account.resources()` lookup | Fire TV is **not in the list** — only the server and the Rokus |

A read-only poll returning 404 against a device that is *currently streaming*
means the server holds no command route to it whatsoever. There is nothing to
send to.

**Dead ends — do not attempt any of these:**

- `account.resource('<name>').connect()` — the Fire TV is not in `resources()`,
  so this raises `NotFound`.
- Any "timeline event" or `PlayQueue`-based variant of `playMedia` — the
  transport is what fails, not the payload.
- `POST /startPlayback` with `target.deviceId` — the server does **not** queue
  playback or wake the client. That claim is false.
- Home Assistant, with any payload shape. Its Plex integration refuses this
  device before sending anything.
- Raw `curl` against the Plex API — same transport, same 404.

**What does work on such a device:** `stop`. Session termination is a *server*
operation (`/status/sessions/terminate`) that never touches the client. Verified
by stopping a live Fire TV stream.

## Failure: "registered but not listening"

The device advertises `player` but nothing answers on its control port. Its Plex
app is closed. Roku only listens on `:8324` while the Plex app is open.

`play` handles this for Rokus automatically: it launches the Plex channel over
Roku's ECP port (`8060`, no auth, answers from the home screen), waits for the
control port to come up, then plays. Cold start to playing is about 15 seconds.
You do not need to do anything.

If waking fails, the Roku did not answer ECP at all, which means **the device is
powered off**. Report that. No software can fix it.

## Failure: command times out

A device that was registered can leave (app closed, powered off) while still
listed. A relayed command then hangs until timeout. Re-run `list_players` to get
current state rather than retrying the same command.

## Failure: nothing matches a search

Speech-to-text mangles titles, so search is deliberately fuzzy. If `play` finds
nothing, try `search` with a shorter fragment of the title. If that also returns
nothing, the library does not have it — say so. Do not offer to play something
adjacent without asking.

## Genre filtering gotchas

Both are handled inside the tools; listed here so results make sense.

- **Multiple genres AND by default.** Plex joins a genre list with `,` meaning
  OR; repeating the parameter means AND. `discover` uses AND when several genres
  are given, because "fantasy epic" means both tags. Pass `match_all=false` to
  widen.
- **Listing endpoints truncate tag lists to two entries.** Raw Plex search
  results will report Toy Story as `['Comedy', 'Animation']` and omit Fantasy.
  Tools that report genres reload the item first, so `discover` and `similar_to`
  return complete lists. Do not conclude a filter is broken because a genre
  looks absent in some other context.

## Library facts

- No collections are defined on this server. Do not build recommendations around
  them.
- Plex's own `similar` field is empty for every item. `similar_to` computes
  overlap from genres instead.
- It is a small library — a few hundred movies and a few dozen shows. Call
  `library_overview` for live counts; do not quote hardcoded numbers, they go
  stale. A request may genuinely have no good match, and saying so is the
  correct answer.
