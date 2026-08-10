# hass-mcp: deliberately not built yet

Four things belong in this server eventually and are absent on purpose. Each is
a genuinely harder problem than lights and blinds, and each fails in a way that
lights and blinds do not. This file records what they need so the decision can
be picked up later without re-deriving it.

Nothing here is blocked on effort. They are deferred because getting them wrong
is expensive, and because building them alongside the easy half means debugging
both at once.

---

## 1. Locks, alarm, and garage

**Why it is not just three more tools.** The agent has access to email and
calendar. That means untrusted text — a calendar invite title, an email subject,
a shared document — reaches the same context that can call tools. "Unlock the
front door" arriving inside a meeting invite is a realistic attack, not a
hypothetical one, and a system prompt saying *don't do that* is not a control.
It is a suggestion to a component whose entire job is following instructions in
text.

**The rule when this is built.** Safety is a property of the tool surface, not
of the prompt:

| Tier | Members | Rule |
| --- | --- | --- |
| Safe direction | `lock_door`, `arm_alarm`, `close_garage` | Always allowed, always verified, always reported. Locking is never the dangerous direction. |
| Gated | `unlock_door`, `disarm_alarm`, `open_garage` | **Absent from `tools/list`** unless explicitly enabled, and even then requiring confirmation through a channel that is not the agent's context. |

Gated tools must be *absent*, not present-and-refusing. A tool the agent can see
is a tool it will try, explain, argue about, and propose workarounds for. A tool
that was never registered produces no such conversation.

**Out-of-band confirmation is the open question.** The natural answer is
[notify-mcp](../notify-mcp/): the server sends a push, waits for a reply, and
only then unlocks. That is a good design and it needs the inbound channel to be
reliable first. Do not build the lock tier before the notify loop has been
running unattended for a while.

**A reasonable interim.** Read-only lock and door state — "the front door is
unlocked", "the back door has been open 40 minutes" — carries most of the daily
value with none of the risk. It is worth doing on its own, and it belongs in
`home_status` rather than in new tools. Note that this still means untrusted
context can *learn* whether the house is secured, which is a smaller problem
than acting, but not zero.

---

## 2. Door and window sensors

Straightforward as data; the difficulty is knowing when to speak.

A contact sensor that reports `open` is not news. "The back door has been open
41 minutes and it is 31°F outside" is news, and the difference is entirely
duration, weather, occupancy, and time of day. Get that judgment wrong in the
noisy direction and the notifications get muted within a week, at which point
the useful alert is also muted. That is the real failure mode — not a missed
event, but a channel nobody reads any more.

Needs, before it is worth building:

- **Duration, not state.** Which means history (§3) or a `last_changed` read,
  and `last_changed` resets on every Home Assistant restart.
- **A quiet-hours and occupancy policy** that lives in a skill, not here.
- **An escalation ladder** — silent, then digest, then push, then urgent — so
  one rule does not have to serve both "window left open" and "water on the
  floor".

Until then, native Home Assistant automations handle the time-critical cases and
handle them better: they are local, they fire in milliseconds, and they work
when the agent is down. The agent should get the cases that need judgment, not
the cases that need speed.

---

## 3. Sensor history and anomaly detection

The most valuable item on this list and the one furthest from done.

Home Assistant's recorder database already holds months of every sensor in the
house, and essentially every LLM integration ignores it in favour of "what is
the temperature right now". History is where the observations live that no
automation will ever produce, because automations are thresholds and this is
trend:

> The office has run 3°F colder than usual every morning this week. Nothing is
> below any threshold, so nothing fired.

That is a failing damper, or a window cracked in March, or a furnace
short-cycling — the kind of thing normally discovered in October by being cold.
Pattern recognition over your own historical data is the strongest argument for
having a language model in the house at all.

Why it is deferred: it is a different shape of problem. Everything in this
server today is a point read or a write with a yes/no confirmation. History is
aggregation over a time series, which means

- **`/api/history/period` is slow and verbose** on any real window. It returns
  every state change, so a week of one sensor is thousands of rows. Either query
  the recorder database directly (fast, but couples this server to Home
  Assistant's schema, which changes between releases) or aggregate carefully
  server-side.
- **"Usual" has to be defined.** Same hour, same weekday, trailing four weeks,
  excluding periods when the house was empty. This is a modelling decision, not
  a plumbing decision, and getting it wrong yields confident nonsense.
- **The output has to be rare.** A tool that reports five anomalies a day is
  reporting noise. It should usually return nothing.

Shape when built: `history(entity, window)` returning aggregates rather than raw
rows, plus `compare_to_baseline(entity)` returning nothing at all on a normal
day. Feed it into the weekly review, never the daily brief — a daily anomaly
report is a daily false positive.

---

## 4. Consumables

Home Assistant already counts blower hours, water flow, pump cycles, and dryer
runs. Nothing surfaces any of it, so filters get changed when someone remembers,
which is late.

This needs no new data source. It is a join between a runtime sensor here and a
`facts` row in [state-mcp](../state-mcp/) — *"furnace filter: 20x25x1, changed
2026-03-14"* — producing:

> Furnace filter is at 340 blower hours. You usually change around 300, and it
> takes 20x25x1.

That is the assistant knowing something about the house that its owner does not,
which is the entire pitch. It is listed last only because it depends on §3 being
solved first — reading a runtime counter is easy, knowing what "usually" means
is not.

**In the meantime**, recurring tasks in state-mcp cover the same ground crudely
and correctly: `task_add title="change furnace filter" recurrence=quarterly`
recurs from when it was actually done. That captures most of the value today and
does not need this server at all.
