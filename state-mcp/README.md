# state-mcp

The household's shared memory. Tasks, shopping, pantry, meal plan, household
appointments, durable facts about the house, a capture inbox, and a log of what
the agent did.

No dependencies — `sqlite3` and the standard library. The protocol half lives in
[../mcpkit.py](../mcpkit.py); this file is only domain logic.

## Why a database and not a notes file

Because it is **shared**. Anyone adds to the shopping list, anyone completes a
task, everyone sees the result, and every write records who did it. "Who said
they'd call the plumber" and "who bought the milk" are answerable. A notes file
gives you none of that and loses a write whenever two people edit at once.

## Who is talking

One bot serves the whole household, so **identity arrives with each message,
not from the environment**. Every write takes an `actor`, and the reliable
thing to pass is the caller's Discord user id:

```bash
python state_mcp_server.py shopping_add item="olive oil" actor=389104857203441664
```

An id nobody has claimed is not guessed at and not refused. It resolves to a
provisional `discord:<id>` person, the write succeeds, and the result says the
account is unlinked. `person_link` then names them **and rewrites everything
they wrote before the link**, so linking late costs nothing:

```bash
python state_mcp_server.py person_link name=Sarah discord_id=389104857203441664
```

That matters because the alternative designs both fail quietly. Refusing makes
a new person's first message an error. Auto-creating a person called
`389104857203441664` gives you a roster of numbers. Holding the write under a
label that reads as what it is keeps it recoverable.

A write with **no** actor is recorded as done by the agent (`hermes`) and the
result says so. That is honest — the agent did do it — and it is visible, which
a silent default never was.

| You have | Use |
| --- | --- |
| A Discord id you do not recognise | `person_identify` — says whether to use a name or ask for one |
| Their name, once they tell you | `person_link` |
| Two entries that are one human (`Sam` / `Smaa`, a leftover `discord:<id>`) | `person_merge` |

`person_merge` moves all thirteen columns that name a person and takes the
linked account with it. Merging a name in isolation would leave the account
pointing at a person who no longer exists, and the next message would recreate
what you just merged away.

Ids are accepted anywhere a name is: `assignee`, `cook`, `who`, and the
`person` filters on `household_digest` and `task_list`. Reads never register
anyone — a mistyped name in a digest comes back as a note, not a new housemate.

## Install

Nothing to install — no dependencies, no venv, no packaging. Run it from the
checkout:

```bash
python E:/hermes-mcp/hermes-tools/state-mcp/state_mcp_server.py state_status
```

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `STATE_DB` | `%USERPROFILE%\.hermes\household.db` | Deliberately not inside the checkout — a `git pull` must never touch the household's memory. **Back it up.** One SQLite file, so a scheduled `cp` is a complete backup. |
| `STATE_AGENT` | `hermes` | What the agent is called in the record. Not a person: it never joins the roster and cannot be assigned a task. |

`STATE_PERSON` is **gone**. It was one value per process and there is one
process for the whole household, so every write that did not name an actor was
credited to whoever configured the server — wrong roughly half the time, and
never visibly. If it is still set, `state_status` says it is being ignored.

Local time matters. Everything user-facing is a **calendar date**, so "due
Friday" stays Friday regardless of the hour. If dates look a day off, the
host's timezone is wrong — fix it there, not with offsets here.

## Tools

| Need | Tool |
| --- | --- |
| Is anything wrong? | `state_status` |
| **What is going on right now** | `household_digest` |
| **What got done, and by whom** | `household_history` |
| Stock up for a recipe | `shopping_add_recipe` |
| Who is in the household | `people_list`, `person_add` |
| **Who is this Discord account** | `person_identify`, `person_link` |
| Two entries, one human | `person_merge` |
| Add / see / finish / drop a task | `task_add`, `task_list`, `task_complete`, `task_drop` |
| Reschedule or reassign a task | `task_update` |
| Shopping list | `shopping_add`, `shopping_list`, `shopping_bought`, `shopping_remove` |
| What is in the house | `pantry_set`, `pantry_low`, `pantry_remove` |
| Meal plan | `meal_plan`, `meal_week` |
| Household calendar | `appointment_add`, `appointment_list`, `appointment_cancel` |
| Facts about the house | `fact_record`, `fact_lookup` |
| Unsorted incoming | `capture_add`, `capture_pending`, `capture_file` |
| What the agent did | `journal_record`, `journal_review` |

`household_digest` is the one that matters. It answers "what's going on" in a
single call, leads with anything wrong, and is what the daily brief is built
from. Without it that question costs fifteen calls and the agent skips some.

Every read returns a `summary` written to be quoted directly, alongside the
structured rows. Reads are for answering questions, not for dumping tables.

## Behaviour worth knowing

**Recurring tasks recur from completion, not from the calendar.** A filter
changed three weeks late is next due three months from when it was *changed*.
Completing a recurring task creates the next one and tells you its id.

**Buying a staple restocks the pantry.** `shopping_bought` updates any matching
pantry row, which is the only reason to keep both tables in sync by hand.

**Correcting a mistake is its own operation.** A typo on the shopping list comes
off with `shopping_remove`, not `shopping_bought` — marking it bought would
restock the pantry and leave the house believing it has something it does not.
Same shape elsewhere: `task_update` to reschedule or reassign rather than drop
and re-add (which loses who created it and when), `appointment_cancel` for a
calendar entry, and `pantry_remove` only for a row recorded in error. Something
that has merely *run out* is `pantry_set qty=0`, which keeps the threshold and
is what puts a staple back on the list.

**Shopping list and pantry are separate on purpose.** The list is what to buy;
the pantry is what is in the house. Collapse them and "we have olive oil" starts
contradicting "olive oil is on the list."

**A recipe does not put salt on the shopping list.** `shopping_add_recipe` takes
a whole ingredient list — measurements, prep notes and all — and adds only what
the house does not have. Three things can spare an ingredient:

| | Meaning | Set by |
| --- | --- | --- |
| `assumed` | The kitchen always has this and nobody counts it | `pantry_set item=salt assumed=true` |
| tracked, in stock | A pantry row with a quantity above zero | `pantry_set item=rice qty=5` |
| already on the list | Someone added it earlier | — |

A tracked staple that is *low* goes on the list even though the pantry has a row
for it: a measurement outranks an assumption. New databases are seeded with a
short assumed list — salt, pepper, olive oil, vegetable oil, vinegar, flour,
sugar, butter, water, ice — and only new ones, so removing something makes it
stay removed. Prune it to taste.

Matching is deliberately conservative. `olive oil` covers `extra virgin olive
oil`, because "extra virgin" describes it. `vinegar` does **not** cover `rice
vinegar` and `butter` does not cover `peanut butter`, because those are
different products, and arriving at the stove without one is worse than one
extra line on a list. Every spared ingredient comes back in `assumed` — quote
it, because that is the only place a wrong assumption is catchable.

`preview=true` answers "do we have everything for carbonara?" without writing.

**Meals are a plan, not a receipt.** `household_history` reports what was *on
the plan*, because nothing in the system records that dinner actually happened.
It also leaves the agent's own work out by default, since "who did what" is a
question about people.

**Unknown people are added, not rejected.** Rejecting means a new housemate
blocks every write until someone runs an admin command. Accepting silently means
`Sam`, `sam`, and `Smaa` become three people. So an unrecognised name is
registered *provisionally*, the result says so, and `state_status` keeps listing
it until someone resolves it — read the `notes` field and fix typos early with
`person_merge`. Register real people with `person_link` and give them aliases;
`cook=Nate` resolves to Nathan.

**Areas are a fixed vocabulary.** `house, kitchen, outside, car, admin, errand,
pets, other`. Free text drifts into "kitchen", "Kitchen", and "the kitchen"
inside a week, after which no filter works. Widen the list in the source when
you genuinely need to.

**`capture_file` should follow a real write.** Marking something filed with
nothing behind it is worse than leaving it unfiled — it disappears from the
inbox and exists nowhere.

## Manual test sequence

Run these in order. Stop at the first failure and read the error; it names the
cause.

```bash
export STATE_DB=$TEMP/household-test.db   # a scratch copy, not the real one
```

```bash
python state_mcp_server.py state_status
```

Now be a person the house has never met. Use a real Discord id if you have one
to hand; any 15–22 digit number works for the test.

```bash
python state_mcp_server.py shopping_add item="oat milk" actor=389104857203441664
```

That should succeed *and* tell you the account is unlinked. Claim it:

```bash
python state_mcp_server.py person_link name=Sarah discord_id=389104857203441664
```

It should report `1 earlier record re-attributed`. Check that it really moved —
`shopping_list` should now say `added_by: Sarah`, not the number. If it does,
identity is working.

```bash
python state_mcp_server.py task_add title="change furnace filter" area=house due=yesterday recurrence=quarterly actor=389104857203441664
```

```bash
python state_mcp_server.py task_complete task_id=1 actor=389104857203441664
```

That last one should report the next occurrence dated three months from today,
not three months from yesterday. If it does, recurrence is working.

```bash
python state_mcp_server.py household_digest
```

Arguments are `key=value`. Quote values containing spaces. Exit code is 0 on
success, 1 on failure, and the JSON body carries the real error text.

## Wire into Hermes

Host paths — Hermes launches MCP servers from its own Python process. See
[deploying](../README.md#deploying).

```yaml
mcp_servers:
  state:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/state-mcp/state_mcp_server.py", "serve"]
```

No environment is needed. `STATE_DB` is omitted on purpose; the default under
`%USERPROFILE%\.hermes\` is the right place. Set it only if you want the
database somewhere specific — a synced folder, say, or a drive you already back
up.

If an older config still sets `STATE_PERSON`, delete the line. It is ignored,
and `state_status` will keep saying so until it is gone.

One instance serves everyone. Nothing about the server is per-person, which is
the point — a second instance per housemate would give each of them their own
shopping list, and then none of this is shared memory.

The skill that drives this server is
[household-state](../skills/household-state/SKILL.md).
