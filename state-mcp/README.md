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
| `STATE_PERSON` | *(unset)* | Who this agent instance speaks for, used when a call does not name an actor. Set it per agent, not per household. Unset means writes are attributed to `unknown`, and `state_status` warns about it. |

Local time matters. Everything user-facing is a **calendar date**, so "due
Friday" stays Friday regardless of the hour. If dates look a day off, the
host's timezone is wrong — fix it there, not with offsets here.

## Tools

| Need | Tool |
| --- | --- |
| Is anything wrong? | `state_status` |
| **What is going on right now** | `household_digest` |
| Who is in the household | `people_list`, `person_add` |
| Add / see / finish / drop a task | `task_add`, `task_list`, `task_complete`, `task_drop` |
| Shopping list | `shopping_add`, `shopping_list`, `shopping_bought` |
| What is in the house | `pantry_set`, `pantry_low` |
| Meal plan | `meal_plan`, `meal_week` |
| Household calendar | `appointment_add`, `appointment_list` |
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

**Shopping list and pantry are separate on purpose.** The list is what to buy;
the pantry is what is in the house. Collapse them and "we have olive oil" starts
contradicting "olive oil is on the list."

**Unknown people are added, not rejected.** Rejecting means a new housemate
blocks every write until someone runs an admin command. Accepting silently means
`Sam`, `sam`, and `Smaa` become three people. So an unrecognised name is
registered and the result says so — read the `notes` field and fix typos early.
Register real people with `person_add` and give them aliases; `cook=Nate`
resolves to Nathan.

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
export STATE_PERSON=Nathan
```

```bash
python state_mcp_server.py state_status
```

```bash
python state_mcp_server.py person_add name=Nathan aliases=Nate
```

```bash
python state_mcp_server.py task_add title="change furnace filter" area=house due=yesterday recurrence=quarterly
```

```bash
python state_mcp_server.py task_complete task_id=1
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
    env:
      STATE_PERSON: "Nathan"
```

`STATE_DB` is omitted on purpose; the default under `%USERPROFILE%\.hermes\` is
the right place. Set it only if you want the database somewhere specific — a
synced folder, say, or a drive you already back up.

The skill that drives this server is
[household-state](../skills/household-state/SKILL.md).
