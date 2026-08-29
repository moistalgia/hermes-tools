---
name: nightly-audit
description: Check what the agent actually did today, verify the changes it claimed to make still hold, and surface silent failures. Use when the nightly audit fires, or when someone asks what you've been doing, whether something actually happened, or why the house is in a particular state.
tags: []
related_skills: []
---

# Nightly Audit

Read back what you did today and check it was true.

Systems like this do not lose trust by failing loudly. They lose it by failing
quietly three times before anyone notices. Read-back verification in the `hass`
server catches failures at the moment of writing; this catches the ones that
reverted an hour later — the bulb that dropped off the mesh, the setpoint an
automation clobbered, the message that never arrived.

Runs late, costs almost nothing, and is the cheapest trust you will ever buy.

## Routine

**1. Read the day.**

```
journal_review days=1
```

**2. Re-check anything that touched the house.** For each house change logged
today, call `room_status` and compare. A setpoint you set to 68 that now reads
64 is the finding — something else moved it, and that is worth knowing whether
it was a person or an automation.

**3. Re-check anything logged `partial` or `failed`.** A light that was
unavailable at 6pm may be back. If it is still down, it has been down all day
and that is a different sentence.

**4. Check the capture inbox.** `capture_pending` — anything sitting there
unfiled for more than a day means the filing loop is not running. That is a
system problem, not a household one, and it is worth saying so plainly.

**5. Log the audit itself.** `journal_record action="nightly audit"` with the
count of what was checked and what was found.

## What to send

**Send nothing on a clean night.** A nightly "all good" is exactly the message
that trains people to stop reading the channel. The audit's value is that it is
silent until it is not.

Send through `notify` only when something is actually wrong, at `priority=low`
so it waits until morning. Nothing found at 11pm needs to wake anybody.

Report it as a fact, not an alarm:

> The kitchen under-cabinet light hasn't responded all day — it was unavailable
> at 6 and it still is. Probably off at the wall. Also, the upstairs setpoint is
> back to 64 after I set it to 68 at four, so something else is moving it.

Two findings is a good audit. If you have eight, the interesting one is that
there are eight — lead with that.

## When something has drifted

**Do not fix it.** Report it. A thing that reverted may have been reverted *by a
person*, and an agent that silently re-applies its own changes overnight is
genuinely unpleasant to live with. State what changed and offer.

The exception is nothing. There is no exception.

## Answering "why is it like this?"

The journal is what makes that question answerable. When someone asks why a
light is on, why the thermostat is where it is, or whether you did something,
check `journal_review` before answering, and quote what you find with the time.

If it is not in the journal, say you have no record of doing it — do not
reconstruct a plausible story. "I don't have a record of that" is a genuinely
useful answer, because it means somebody or something else did it.
