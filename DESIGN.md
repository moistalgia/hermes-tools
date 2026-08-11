# hermes-tools design

How the tools in this repo fit together, the rules every server here follows,
and what is deliberately not built yet.

Written after `plex-mcp` worked, because everything that made it work
generalizes. Each server below follows the same shape, and the shape is the
point: **action → MCP tool → skill**, with a semantic layer in between so that
nothing above the server ever names a device.

---

## 1. Three layers

| Layer | What lives there | Changes when |
| --- | --- | --- |
| **[mcpkit.py](mcpkit.py)** | The protocol. Tool registry, JSON-RPC over stdio, CLI dispatch, argument coercion. Knows nothing about any domain. | Never, in practice |
| **MCP server** | Deterministic capability against one external system. Resolves human names to identifiers, acts, verifies, and fails in language the agent can act on. | The hardware or the API changes |
| **Skill** | Judgment and policy. What "wind down" means, how much to confirm, what a good brief looks like, when to ask. | Your preferences change |

The value of the split is that policy stays in version-controlled prose you can
edit in thirty seconds, instead of being hardcoded in Python or buried in a
prompt. A new household routine is a markdown file, not a new server.

The failure mode to avoid is a skill reaching around its server — writing `curl`
because a tool returned something awkward. Every skill here says so outright, in
the same words the Plex skill uses: *if a tool fails, the fix is never a
different transport*. A server whose tools get bypassed is a server whose
guarantees are fiction.

**The agent does not build its own tooling.** Servers are written on the host,
committed, and pulled. An agent that edits its own MCP servers is one `git pull`
away from losing the change and has no way to test it. Deployment docs are for
the human running the host.

---

## 2. Conventions

Not style preferences. Each exists because its absence is a specific way these
systems misbehave.

**One file of domain logic per server, plus `mcpkit`.** No MCP SDK, no wrapper
framework. The protocol is ~250 readable lines and a framework would hide the
layer that breaks most often. `plex-mcp` predates `mcpkit` and carries its own
copy; that is fine and not worth churning working code over.

`prowlarr-mcp` carries a copy too, and that one is on purpose: it is meant to be
copied out of this repo as a directory and dropped into an MCP folder by itself,
which a `../mcpkit.py` import makes impossible. The duplication is only
tolerable because it is checked — a test compares the two files, and CI runs the
server from a scratch directory with no repo above it. An unchecked copy would
mean a server that behaves differently deployed than it does under test, which
is the worst available place for a difference to live. Do not add a third
without the same guards.

**Test the things that fail quietly.** A tool that errors is a tool someone
fixes. The ones worth tests are the ones that return a confident wrong answer:
date arithmetic, room resolution, partial writes. [tests/](tests/) is standard
library only, and its fake Home Assistant can be *deaf* — accepting every call
and changing nothing — because that is the failure §3 exists to catch and the
one you cannot stage on real hardware.

**Every tool is also a CLI subcommand, through the same dispatch path.** The
highest-leverage convention in the repo. You prove a call works from a shell,
the agent makes the identical call over MCP, and there is exactly one place it
can be breaking. Do not build a server without this.

**The agent never sees a raw identifier.** Not `light.hue_color_lamp_3`, not a
Plex machine ID, not a database row id it did not just receive. Rooms and
things, resolved inside the server against a map. This is the `PLEX_ALIASES`
idea generalized, and the reasoning in that comment — *Plex names are frequently
useless and a room name outlives the hardware in it* — is even truer of Zigbee
bulbs. Replace a bulb, edit one line, every prior conversation still works.

**Curate the surface.** A Home Assistant instance has hundreds of entities.
Expose the forty you would ever address out loud. An entity the agent cannot see
is an entity it cannot get wrong, and a tool list it can hold in its head is one
it uses correctly.

**Every server has a discovery tool.** `list_players` in `plex-mcp`,
`discover_entities` in `hass-mcp`, `people_list` in `state-mcp`. Curation only
works if there is a sanctioned way to find out what exists — otherwise the first
unmapped device sends someone to `curl`, and the map stops being the single
place identifiers live. Discovery is read-only, returns something ready to paste,
and refuses the domains the server does not address rather than listing things
that cannot be used.

**Configuration reloads itself.** A map that needs a restart to take effect is a
map that silently runs stale, because the old one keeps working and the new
device is merely absent. `hass-mcp` stats `house.json` on each call. Anything
edited as often as a device list should do the same.

**Verbs, not a dispatcher.** `set_cover(room, position_pct)`, never
`call_service(domain, service, data)`. A generic service call re-imports every
guess the map just eliminated: the agent has to know the domain, the service
name, and the payload shape, and it will get one of them wrong on a device you
never tested.

**Absolute values only.** No `dim`, no `warmer`, no `+10`. Relative operations
require reading state first, and forcing that into two visible steps means the
reasoning is in the transcript instead of guessed at.

**One direction, stated twice.** Where a number could be read either way, the
tool description says which way it goes and the skill converts before calling.
`position_pct` is how *open* a blind is, and people say the opposite at least
as often — "75% closed" is 25. Two tools with opposite conventions for the same
quantity is a mistake waiting for a tired evening, so `all_covers` measures the
same direction as `set_cover`. The skill also owes the user an answer in *their*
frame: "three-quarters down", not "25%".

**Every write surface needs an undo.** Not a transaction log — a tool for the
ordinary case of having got it wrong. Without `shopping_remove`, taking a typo
off the list means marking it *bought*, which restocks the pantry and leaves
the house believing it has something it does not. An add-only surface does not
prevent corrections; it just routes them through whichever tool is closest,
and that tool has side effects.

**Read back after every write.** See §3.

**Errors teach.** `ToolError` exists so the agent reads a sentence, not a status
code:

| Bad | Good |
| --- | --- |
| `404` | `Fire TV cannot be a playback target (no 'player' capability). Pick another device.` |
| `{"error": "not found"}` | `No room named "back room". Known rooms: office, kitchen, theater, bedroom. Add an alias if this is a new name for one of them.` |
| `KeyError: covers` | `The kitchen has no covers in the map. This is final unless you add them — do not try another room or another approach.` |

An error that names the cause and the next move is how the agent stops flailing.
One that does neither is how you get four wrong tools tried in a row.

**An empty result is a diagnosis, not a value.** Zero rows because nothing
matched, zero rows because a filter removed everything, and zero rows because
every backend the query touched is down are identical over the wire and need
opposite responses — try another title, loosen the filter, stop and report.
`prowlarr-mcp` asks *why* before returning empty and says which one it is,
because "nothing found" sends an agent hunting for a better query against a
system that is simply broken. Any tool that can return an empty collection for
more than one reason owes the caller the reason.

**Error payloads list plausible options only.** `resolve_player` filters to
`relevant` devices because *naming all 18 registered browser tabs teaches the
agent nothing and invites it to try them*. Same rule for rooms, areas, people,
and task ids.

**Fixed vocabularies where free text would drift.** Task areas are a closed
list. Priorities are four words. Free text becomes "kitchen", "Kitchen", and
"the kitchen" inside a week, after which no filter works.

---

## 3. Read-back verification

Home Assistant returns `200 OK` when a service call is *dispatched*, not when
anything happened. A Zigbee bulb switched off at the wall, a Z-Wave lock with a
dead battery, and a working device are indistinguishable from the response. This
is the single biggest reason generic integrations feel unreliable: they report
success for actions that did not occur.

Every write tool follows the same shape — `confirm_playback` from `plex-mcp`,
generalized:

1. Resolve the human name to identifiers. Fail here with a named cause.
2. Dispatch.
3. Poll until state matches the request, or until timeout.
4. Report **actual** state, and whether it was confirmed.

| Outcome | What the agent gets |
| --- | --- |
| Confirmed | `office lights → on, 40% (confirmed)` |
| Timed out | `command accepted but nothing changed. light.office_lamp is unavailable (off at the switch, or off the mesh)` |
| Partial | `kitchen lights → on, 60% — 1 of 2 confirmed. light.kitchen_under_cabinet is unavailable` |

The partial case matters more than it looks. Rooms are groups, and a group with
one dead member is what a real house looks like. Reporting "kitchen lights on"
when one of four is missing is how you train someone to stop trusting the
assistant.

Timeouts are per-domain and cheap to get wrong. Lights confirm in under a
second; blinds travel for half a minute; thermostats never converge, so climate
confirms that the **setpoint** changed and explicitly not that the room is warm.

---

## 4. What exists

| Server | Covers | Depends on |
| --- | --- | --- |
| [plex-mcp](plex-mcp/) | Media playback | Plex, `plexapi` |
| [state-mcp](state-mcp/) | Shared household memory | SQLite. Nothing else |
| [notify-mcp](notify-mcp/) | Push out, messages in | ntfy / Telegram / Pushover |
| [discord-mcp](discord-mcp/) | DM one named person, not the home channel | Discord, same bot token as Gladys |
| [hass-mcp](hass-mcp/) | Lights, blinds, tilt, thermostats, scenes | Home Assistant |
| [prowlarr-mcp](prowlarr-mcp/) | Indexer search, returning magnets | Prowlarr |
| [qbt-mcp](qbt-mcp/) | Starting a download, and confirming it | qBittorrent |

Plus [tests/](tests/) and [scripts/backup_state.py](scripts/backup_state.py),
neither of which the agent ever touches — they are for the human running the
host.

| Skill | Drives |
| --- | --- |
| [plex-media-playback](skills/plex-media-playback/) | `plex` |
| [media-acquisition](skills/media-acquisition/) | `prowlarr`, `qbt` |
| [home-control](skills/home-control/) | `hass` |
| [household-state](skills/household-state/) | `state` |
| [meal-planning](skills/meal-planning/) | `state` |
| [daily-brief](skills/daily-brief/) | all of them |
| [nightly-audit](skills/nightly-audit/) | `state`, `hass`, `notify` |
| [email-triage](skills/email-triage/) | email, `state` |

---

## 5. state-mcp: the shared layer

The least glamorous server and the one that unlocks the most. Meal planning,
house todos, and a brief worth reading all fail the same way without it: the
agent cannot remember that chili is planned for Thursday, that the filter was
changed in March, that the olive oil ran out, or that the gutters have come up
twice.

Meal planning is not a hard reasoning problem. It is an inventory problem plus a
recipe corpus. The intelligence was never the missing piece.

**It is shared, and that is the design.** Anyone adds to the shopping list,
anyone completes a task, everyone sees the result, and **every write records who
did it**. "Who said they'd call the plumber" and "who bought the milk" are
answerable. A notes file gives none of that and loses a write whenever two
people edit at once.

| Table | Holds |
| --- | --- |
| `people` | The roster, with aliases. `cook=Nate` resolves to Nathan |
| `tasks` | Chores, with area, assignee, due date, and recurrence |
| `shopping` | What to buy |
| `pantry` | What is in the house |
| `meals` | The plan, with who is cooking |
| `appointments` | Household-visible, deliberately not anyone's work calendar |
| `facts` | Everything true about the house that lives nowhere else |
| `capture` | Unsorted incoming, before anyone decides what it is |
| `journal` | What the agent did, and whether it worked |

Three of those deserve their reasoning stated.

**`shopping` and `pantry` are separate on purpose.** The list is what to buy;
the pantry is what is in the house. Collapse them and "we have olive oil" starts
contradicting "olive oil is on the list". They are linked in one direction only:
buying a staple restocks it.

**`capture` exists because the reason personal systems die is not effort, it is
the decision of where a thing goes.** Task or note? Grocery or pantry? One
inbox, no destination required, agent does the filing later. Inbound messages
from a phone land here, which is why `notify-mcp` and `capture` were built
together — inbound messaging *is* the capture endpoint.

**`journal` is the episodic log.** Two payoffs, and the second is the real one:
"why is the thermostat at 64?" becomes answerable, and a nightly pass over the
table is how the system finds its own silent failures without anyone doing the
noticing. It is also the substrate for eventually learning that a light it
turned on gets turned off four minutes later.

**Recurrence anchors on completion, not the calendar.** A filter changed three
weeks late is next due three months from when it was *changed*. Storing a fixed
schedule instead produces a task that is permanently overdue and therefore
permanently ignored.

---

## 6. Proactivity

The difference between a tool and an assistant is whether it ever starts the
conversation. That needs two halves: something that fires runs on a schedule,
and somewhere to put the output that gets read. Email is where reminders go to
die.

[notify-mcp](notify-mcp/) is the second half, and it is bidirectional where the
backend allows. Being able to text "add olive oil to the list" from the shop is
the single largest jump in how much a system like this gets used — it stops
being something you sit down at.

Transport and meaning stay separate. `inbox_fetch` returns messages and files
nothing; the skill decides what a message *is* and writes it through `state`.
Otherwise `notify-mcp` quietly becomes a second, worse state store.

**A scheduled job's delivery target and a tool call are not the same
channel.** `hermes cron ... --deliver discord` posts the run's final response
to the configured home channel regardless of what the skill did during the
run — so a skill that correctly calls a DM tool can still have its output
echoed into a channel the whole household shares, if the cron job is also
configured to deliver there. [discord-mcp](discord-mcp/) exists because
Gladys' own reply-on-originating-surface and Hermes' channel delivery are
both room-shaped, and a daily brief is not a household announcement — it is
addressed to the person it was written for. Bot-to-bot DMs are blocked by
Discord itself; bot-to-*user* DMs are not, so this needed no more than a
closed, named recipient list and two API calls.

Two rules that protect the channel, both enforced in skills rather than code
because they are judgment:

- **The brief is never `urgent`.** Urgent bypasses quiet hours and there is
  exactly one such signal to spend.
- **The nightly audit sends nothing on a clean night.** A nightly "all good" is
  the message that trains people to stop reading the channel.

---

## 7. What does not belong to the agent

Anything time-triggered, sensor-triggered, latency-sensitive, or safety-critical
stays a native Home Assistant automation. The agent is the **interface** and the
**exception handler**, not the control loop.

| Belongs in HA | Belongs to the agent |
| --- | --- |
| Porch light at sunset | "You're heading out and the upstairs blinds are open" |
| Away mode on last-person-leaves | "The office has run colder than usual all week" |
| Freeze protection | "The back door has been open 40 minutes and it's 31°F" |

If you find yourself writing a polling loop inside an MCP server, that is an
automation wearing a disguise. Move it.

---

## 8. Deliberately not built

Recorded so the decisions do not have to be re-derived. Detail in
[hass-mcp/FUTURE.md](hass-mcp/FUTURE.md).

| | Why deferred |
| --- | --- |
| **Locks, alarm, garage** | The agent reads untrusted text — emails, invites, captured messages. "Unlock the front door" inside a calendar invite is a realistic path, and a system prompt is not a control. Safety has to be a property of the tool surface: gated tools **absent** from `tools/list`, not present-and-refusing. Needs out-of-band confirmation, which needs the notify loop proven first. |
| **Door and window sensors** | Easy as data, hard as judgment. A contact reporting `open` is not news; forty minutes at 31°F is. Get the threshold wrong in the noisy direction and the channel gets muted within a week — including the alerts that matter. Needs duration, occupancy, and quiet hours first. |
| **Sensor history / anomalies** | The highest-value item on the list and the furthest from done. Months of recorder data sit unused, and trend is where the observations live that no threshold will ever produce. Deferred because it is a different shape of problem — aggregation over a time series, plus defining what "usual" means, plus the discipline to report nothing on a normal day. |
| **Consumables** | Filters by blower hours, salt by cycles. Needs no new data source — it is a join between a runtime sensor and a `facts` row — but it needs "usual" solved first. Recurring tasks cover most of the value today. |
| **Finance** | Not wanted. |
| **Automated ordering** | The demo everyone asks for. Brittle scraping, real money, low trust. 90% of the value is a correct list on a phone at the shop, and that already works. |
| **Agent-owned scheduling** | Letting the agent decide *when* to do house things, rather than on request or on an HA schedule. Produces a house that behaves unpredictably for reasons nobody can reconstruct. Keep the control loop dumb. |

One thing on this list is worth doing sooner than its position suggests:
**read-only lock and door state** — "the front door is unlocked", "the back door
has been open a while" — carries most of the daily value with none of the acting
risk, and belongs in `home_status` rather than in new tools.

---

## 9. Order

1. **`state-mcp`** — boring, small, unlocks everything downstream.
2. **`notify-mcp` + one scheduled job** — prove the loop with a trivial message
   before investing in brief quality. The loop is where the breakage is.
3. **`hass-mcp` read-only** — `home_status` into the brief. Learn the vocabulary
   before write paths depend on the alias map. You will find out that you say
   "the back room" and HA calls it `area.office_2`.
4. **`hass-mcp` writes** — lights, blinds, climate, with §3 verification.
5. **Skills** — meal planning, household state, brief, audit.
6. **§8, when the rest has been running unattended for a while.**

Steps 3 and 4 are in that order on purpose.

---

## 10. Open questions

- ~~**Is `household.db` backed up?**~~ Answered:
  [scripts/backup_state.py](scripts/backup_state.py) takes verified, rotating
  snapshots, and `--check` reports when the newest one has gone stale — because
  a scheduled job that quietly stopped running looks exactly like one that is
  working. It uses SQLite's backup API rather than `cp`: copying a live
  database can capture a torn page, and the copy looks fine until the day you
  need it. **Still needs scheduling on the host**, which is the half a script
  cannot do for itself.
- **One agent or several?** A single agent with every server attached has a
  large tool surface and gets vaguer as this grows. Worth revisiting now that
  there are four servers and seven skills.
- **Who edits `house.json`?** It lives outside the checkout, so the agent
  *could* write to it — `discover_entities` already hands back a paste-ready
  snippet, which is one step short. Letting the agent edit its own semantic map
  is a real convenience and a real way to lose a hand-tuned file. Undecided.
- **A physical surface.** The honest answer to "ingrained in daily life" is that
  an assistant you open an app to reach gets used twice a week. A wall tablet in
  the kitchen showing the brief and what is off in the house changes the usage
  pattern more than any capability here. Mostly a hardware and layout problem —
  Home Assistant already serves dashboards.
