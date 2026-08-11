# Changelog

Notable changes, newest first. The reasoning behind the conventions these
follow lives in [DESIGN.md](DESIGN.md).

## Unreleased

### Added

- **`discord-mcp`** — one tool, `discord_dm`, so a scheduled run can message
  the person it was written for instead of the shared home channel a cron
  job's `--deliver discord` posts to regardless of what the skill did.
  Bot-to-bot DMs are blocked by Discord itself (error 50007); bot-to-*user*
  DMs are not, so this needed no more than the same bot token Gladys already
  runs on. Recipients are a closed, named list read from `DISCORD_DM_USERS`
  — the agent says `user="nathan"`, never a raw id, same rule this repo
  already applies to rooms and Plex players. The `daily-brief` skill now
  sends here instead of the home channel.
- **`household_history`** — "who did what this week" in one call: chores
  finished, shopping bought, meals planned, tasks dropped, with a per-person
  tally. The data was always recorded and mostly unreadable: `bought_by` and
  `bought_at` had been written since the beginning and **no tool ever read them
  back**, and completed tasks were reachable only through `task_list
  include_done` on a hardcoded seven-day window. Meals are reported as *planned*
  rather than cooked, because nothing in the system records that dinner
  happened, and the agent's own work is excluded by default — "who did what" is
  a question about people.
- **`shopping_add_recipe`** — hand it a whole ingredient list, measurements and
  prep notes included, and it adds only what the house does not already have.
  The failure mode it exists to prevent is a nineteen-item list that includes
  salt, because that list gets ignored and then the list itself is lost.
  - **An ingredient is spared three ways**: a new `assumed` pantry flag ("the
    kitchen always has this and nobody counts it"), a tracked pantry row with a
    quantity, or being on the list already. A tracked staple that is *low* goes
    on the list anyway — a measurement outranks an assumption.
  - **Matching is by whole-word suffix, and the prefix must be a qualifier.**
    Suffix alone was the first attempt and reads as obviously correct: `olive
    oil` covers `extra virgin olive oil`. It also silently made `vinegar` cover
    `rice vinegar` and `butter` cover `peanut butter` — dropping the one
    ingredient the dish was about. "Extra virgin" describes olive oil; "rice"
    makes vinegar a different bottle.
  - **Every spared ingredient is returned in `assumed`.** The rule cannot tell
    whether the vinegar in the cupboard is the right vinegar, so the assumption
    is always visible rather than silent.
  - New databases are seeded with a short assumed list; existing ones are not,
    because re-seeding would resurrect items a household had deliberately
    removed and salt would reappear months later with nothing to explain it.
  - `preview=true` answers "do we have everything for carbonara?" without
    writing. `shopping.for_dish` records why an item is on the list, which is
    the question asked in the shop in front of the shelf.

### Fixed

- **A pantry row at zero counted as having some.** Anything not flagged a staple
  was treated as in stock regardless of quantity, so an ingredient someone had
  carefully recorded running out of was spared from the shopping list.
- `household_history`'s queries tie-break on `id`, so two chores completed in
  the same second come back in a stable order rather than an arbitrary one.

### Changed

- **`state-mcp` learned who is talking.** The store was built as household
  memory but identified people the way a single-user tool does: `STATE_PERSON`,
  one value read from the environment at startup. One bot instance serves the
  whole household, so that value was a constant — every write that did not
  name an actor was credited to whoever configured the server, regardless of
  who was speaking. Nothing about that failure is visible. The shopping list
  looks correct; it is just wrong about who wanted what, and stays wrong.

  Identity now arrives with the message. `actor` accepts a Discord user id
  anywhere it accepts a name, resolved through a new `identities` table.
  - **An unclaimed account is held, not guessed and not refused.** It resolves
    to a provisional `discord:<id>` person and the write succeeds, because
    refusing would make a new housemate's first message an error, and inventing
    a person named `389104857203441664` gives you a roster of numbers.
    `person_link` then names them *and rewrites everything they wrote before
    the link*, which is what makes linking late free.
  - **`person_merge` moves all thirteen columns that name a person, and the
    linked account with them.** Leaving the account behind meant the next
    message from it rebuilt the row the merge had just deleted — a correction
    that silently undid itself and had to be made again forever.
  - **A write with no actor is recorded as the agent (`hermes`), and says so.**
    True, and visible. The old default was neither.
  - **Reads no longer register people.** `household_digest`, `task_list` and
    `appointment_list` resolved their person filter through the write path, so
    a mistyped name in the most-called tool in the server quietly created a
    housemate. They now report an unknown name instead.
  - `state_status` lists accounts nobody has claimed, and says outright that
    `STATE_PERSON` is being ignored if it is still set. Existing databases
    migrate in place on open.
  - New: `person_identify`, `person_link`, `person_merge`.

### Fixed

- **A yearly recurring task completed on 29 February raised `ValueError`.**
  `add_interval` clamped month arithmetic but not years, so 29 Feb + 1 year was
  an exception rather than 28 Feb. It failed *after* the task had been marked
  done, leaving a completed task with no next occurrence — a recurring chore
  that silently stopped recurring, which nothing looks wrong about until it
  never comes back.
- **`task_complete` and `shopping_bought` are now atomic.** Each writes more
  than once and `write()` commits per statement, so an interruption part-way
  through left half a change behind. A `transaction()` helper covers both.
- **List tools dropped their collection key when the collection was empty.**
  `ok()` stripped `[]`, so `shopping_list` returned no `items` field on an
  empty list — the commonest case, and the one a caller is most likely to have
  forgotten to special-case. Empty collections now keep their shape; only
  `notes` and `warnings` are dropped when empty, being annotations rather than
  data.
- **`set_cover` blamed the wrong thing for a missing entity.** An entity Home
  Assistant does not have reported zero features and was sorted in with the
  two-state blinds, producing "it can only open and close" about something that
  does not exist. Missing entities are now named as missing, and a request that
  is partly serviceable no longer reports clean success.
- **`set_lights` confirmed brightness but never colour temperature.** A
  fixed-white bulb accepts `color_temp_kelvin` and ignores it, so a colour that
  never changed was reported as confirmed.
- Documentation, docstrings, and several **error messages** described a
  containerised deployment that no longer exists — including advice to "check
  from inside the container", which is the wrong instruction at exactly the
  moment someone is debugging a connection. Server defaults for `STATE_DB` and
  `HASS_MAP` disagreed with their own docstrings.

### Added

- **`prowlarr-mcp`** — one search across every configured indexer, returning
  magnet links. Three tools: `prowlarr_status`, `list_indexers`, `search`.
  Search only, deliberately — no grab, no download client, no torrent state. The
  magnet goes to whatever handles fetching, and a search tool that can also
  start downloads is a much wider blast radius than the job needs.
  - **Prowlarr owns the indexers; this owns the answer.** Credentials, indexer
    definitions and the proxy for challenged indexers all live in Prowlarr. None
    of that is reimplemented here, and a failing indexer is reported as a
    failing indexer rather than worked around.
  - **A magnet, or an explanation.** Prowlarr's `magnetUrl` is null for a great
    many indexers — often for every result of a search — and what it returns
    instead is a proxy link to a `.torrent`. The magnet is recovered in four
    stages: `magnetUrl`, then `infoHash`, then the indexer's own link
    Base64-encoded inside Prowlarr's proxy URL (free, and the common case),
    then fetching the download link and either following its redirect to a
    magnet or computing the info hash from the `.torrent` itself. Only the last
    touches the network, only for the releases being returned, four at a time.
    The hash is taken over the `info` dictionary's bytes exactly as they
    arrived — re-encoding would produce a different hash for any file whose
    encoder ordered keys differently, and that hash would be silently wrong
    rather than obviously broken. A `.torrent` link is never returned in the
    magnet slot; everything downstream takes a magnet, and a near-miss surfaces
    three steps later with nothing pointing back at the cause.
  - **Titles are parsed into fields** — resolution, source, codec, HDR, size,
    seeders, age — so choosing happens against data. Cam rips are detected and
    ranked last regardless of seeder count.
  - **Empty results carry their reason.** Nothing matched, everything filtered
    on seeders, and every indexer failing are one value over the wire and need
    three different responses.
  - Ships its own copy of `mcpkit.py` so the directory is drop-in, with a test
    asserting the copy has not drifted and a CI step running it from a scratch
    directory with no repo above it.
  - Includes `docker-compose.yml` for Prowlarr and its solver, and
    `HERMES_PROMPT.md` for the one-time bootstrap.
- **The `media-acquisition` skill** — query hygiene, how to choose between
  releases, and the `!fetch` handoff, which asks before it sends anything.
- **`tests/`** — 152 stdlib `unittest` tests, no dependencies. Includes a fake
  Home Assistant that can be *deaf* (accepts every call, changes nothing), which
  is the only way to test read-back verification without a bulb you can switch
  off at the wall on demand. `test_serve.py` runs each server as a subprocess
  and talks JSON-RPC to it.
- **`.github/workflows/ci.yml`** — compile, test, and assert that usage output
  never reaches stdout, on Linux and Windows.
- **`scripts/backup_state.py`** — verified, rotating backups of `household.db`,
  answering the open question DESIGN.md had been asking. Uses SQLite's backup
  API rather than a file copy, because copying a live database can capture a
  torn page that looks fine until you need it.
- **`hass-mcp`: `set_cover_tilt`** — venetian slat angle, a separate axis from
  height. A blind can be fully down and still let all the light in, so "closed"
  was two questions with only one tool.
- **`hass-mcp`: `all_lights` and `all_covers`** — "close all the blinds" as one
  call, reporting room by room. Previously an unstated fan-out over
  `list_rooms` with no defined shape for a partial result.
- **`hass-mcp`: `hass_status` now catches unreadable room keys.** `"light"`
  instead of `"lights"` maps nothing, the room still resolves, and the bulb is
  merely absent — the hardest kind of wrong to notice.
- **`state-mcp`: `task_update`, `shopping_remove`, `pantry_remove`,
  `appointment_cancel`.** The write surface was add-only, so correcting a typo
  on the shopping list meant marking it *bought*, which restocks the pantry and
  leaves the house believing it has something it does not.
- **`mcpkit`: `n()`**, a number type that keeps fractions.
- **LICENSE** (MIT) and this changelog.

### Changed

- **`set_thermostat` accepts fractional targets.** It was integer-only, which
  made 20.5° unreachable — normal in Celsius. Confirmation allows half a step
  of snap and reports the setpoint it actually settled at.
- **`set_cover`'s description states the direction twice.** `position_pct` is
  how *open* a blind is; people say the opposite at least as often, and "75%
  closed" is 25. `all_covers` uses the same direction on purpose.
- **The `home-control` skill** now has a conversion table for blind positions, a
  rule to answer in the user's frame rather than the tool's, conventions for
  "halfway" and relative changes, and guidance on when tilt is meant.
- `plex-mcp`'s default `PLEX_URL` is `http://127.0.0.1:32400`, matching how it
  actually runs.
