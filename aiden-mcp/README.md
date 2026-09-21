# aiden-mcp

Brews coffee on a Fellow Aiden, and confirms it actually started.

Talks to Fellow's cloud API directly — the brewer has no local API, so every
call goes out to the internet and back. Standard library only. No dependencies,
no venv.

## What the brewer will and will not let you do

This shapes the whole tool surface, so it is worth reading before wiring it up.

**You can choose the recipe, through a side door.** `/start` itself takes no
arguments — it brews whatever Instant Brew preset the machine has selected. So
the recipe is a separate write that happens first, which is what
`set_instant_brew` does, and what `brew_now(profile=...)` does on your behalf.

That write was not supposed to work. Fellow's gateway rejects its own mobile
client's dedicated selected-profile route for a live Aiden, which is why the
Home Assistant integration exposes no such control. The generic
`PATCH /devices/{id}` carrying `ibSelectedProfileId` is a different door to the
same setting, and against a real brewer it takes. Nothing here relies on that
staying true: the selection is read back off the machine every time, and a write
that changed nothing is reported as a failure naming the preset that survived.
`brew_now` will not start a brew whose selection it could not verify — brewing
the wrong recipe under the right name is worse than brewing nothing.

Changing the preset also changes what the **button on the machine** brews. That
is the same setting, not a remote-only one.

**Scheduled brews never depended on any of this.** A schedule carries its own
recipe and water volume. Fellow caps them at **10 per brewer** and refuses the
eleventh; `schedule_brew` catches that and lists what is already there so you
can decide what to drop.

**There is no remote stop.** A stop route exists in the mobile client and was
observed failing to cancel a live brew, so it is not exposed here — a stop
button that does not stop anything is worse than no button. Once a brew starts,
it finishes at the machine.

**The brewer still needs loading by hand.** Coffee in the basket, water in the
reservoir, carafe underneath. Nothing here can do that, and `brew_now` checks
all three before it dispatches.

## Setup

### 1. Firmware

Remote start requires **1.5.16 or newer**. Below that the cloud accepts the
command and the brewer ignores it, which is the worst possible combination.
Update in the Fellow app. `brew_status` reports the version and refuses to
start on an older one rather than failing silently.

### 2. Nothing — the preset is settable from here

`set_instant_brew` and `brew_now(profile=...)` both write it, and `brew_status`
reports what is currently loaded so Hermes can say what it is about to make
before it makes it.

The preset lives in `ibSelectedProfileId` / `ibWaterQuantity`. The
similarly-named `brewingProfileId` / `brewingWaterVolumeMl` are the brew that is
running or last ran — the second is `last_brew_volume` in Home Assistant's
integration — and reading either as "what happens next" gives a confident wrong
answer with a real number attached. `brew_status` reports both, separately.

### 3. Hermes

```yaml
  aiden:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/aiden-mcp/aiden_mcp_server.py", "serve"]
    env:
      FELLOW_EMAIL: "<the Fellow app account>"
      FELLOW_PASSWORD: "<its password>"
```

Same credentials as the Fellow mobile app. They are exchanged for a bearer
token on first use and kept in memory only — nothing is written to disk, and
no token is ever logged.

| Variable | Default | Notes |
| --- | --- | --- |
| `FELLOW_EMAIL` | — | Required. |
| `FELLOW_PASSWORD` | — | Required. |
| `FELLOW_BREWER_NAME` | — | Only needed if the account has more than one device. With several and none named, every tool refuses rather than operating the wrong machine. |
| `FELLOW_CONFIRM_TIMEOUT` | `25` | Seconds to wait for the brewer to report it started. |
| `FELLOW_TIMEOUT` | `30` | Per-request HTTP timeout. |
| `FELLOW_BASE_URL` | Fellow v2 | Only for tests. |

## Tools

| Tool | Does |
| --- | --- |
| `brew_status` | Live state: brewing or not and at what stage, ready or not, what recipe is loaded, and what is physically in the way. |
| `brew_now` | Brews a named recipe, or the loaded one. Safety check, select-and-verify, then confirms the brew started. |
| `set_instant_brew` | Changes which recipe and volume the Instant Brew preset uses — for the button as well as for `brew_now`. |
| `list_profiles` | The saved recipes and their parameters. |
| `create_profile` | Saves a new recipe. Only a title is required. |
| `delete_profile` | Removes one. Refuses if a schedule depends on it. |
| `import_profile` | Saves a shared recipe from a brew.link URL. |
| `list_schedules` | The scheduled brews, in human terms. |
| `schedule_brew` | Adds a recurring brew at a fixed time, with its own recipe and water. |
| `set_schedule_enabled` | Pauses or resumes one without deleting it. |
| `cancel_schedule` | Deletes one. |

Every tool is also a CLI subcommand through the same dispatch path:

```bash
python aiden_mcp_server.py brew_status
```

```bash
python aiden_mcp_server.py schedule_brew time=06:45 days=weekdays profile="Morning Ethiopian" water_ml=950
```

```bash
python aiden_mcp_server.py brew_now profile="Light Roast"
```

Times are 24-hour `HH:MM` in the brewer's own timezone. Days are `daily`,
`weekdays`, `weekends`, or a list like `mon,wed,fri`. Water is millilitres,
150–1500. Recipes are addressed by title, never by the `p7`-style ids the API
uses.

## Read-back

Per DESIGN.md §3, every write is verified rather than reported from the
response:

| Outcome | What the agent gets |
| --- | --- |
| Confirmed | `Brewing Morning Ethiopian, 320ml (confirmed, phase: bloom).` |
| Not confirmed | `Fellow accepted the start … but the brewer has not reported brewing after 25s. Check brew_status before starting another.` |
| Blocked | `The brewer is not ready: The lid is open. Close it.` |

The middle row is the one that matters. Fellow's cloud returns 200 for a start
the machine never acts on — that is [FellowAiden-HomeAssistant #48](https://github.com/kristofferR/FellowAiden-HomeAssistant/issues/48),
and reporting it as success is how someone comes downstairs to an empty carafe.

**A start is never retried**, at any status, including a dropped connection.
Every other call in here retries on 5xx; this one cannot, because a request
whose response was lost may well have started a brew, and there is no remote
stop to undo a duplicate with. On an ambiguous failure the tool says so and
tells the agent to call `brew_status` rather than guess.

## Credit, and why this is not a wrapper

Fellow publishes no API. The endpoints, headers, validation rules and safety
preconditions here were all learned from two projects that reverse-engineered
the mobile app:

- [9b/fellow-aiden](https://github.com/9b/fellow-aiden) — the original Python library
- [kristofferR/FellowAiden-HomeAssistant](https://github.com/kristofferR/FellowAiden-HomeAssistant) — the Home Assistant integration, and the source of the v2 notes and the `can_start_brew` gate

This is an independent implementation against the same interface rather than a
wrapper around either, for four reasons:

1. **Remote start is v2 only.** `fellow-aiden` targets `/v1`, which has no start
   route at all — it cannot start a brew, which is the main thing wanted here.
2. **It logs its own bearer tokens.** It hardcodes `DEBUG` and prints the parsed
   login response — access and refresh token included — to a handler bound to
   `sys.stdout`, which in an MCP server *is* the JSON-RPC transport. `mcpkit`
   repoints stdout to stderr first, so the result would be tokens in a log
   rather than a broken stream, but a credential leak prevented by an unrelated
   defence is not a property to depend on.
3. **Its profile model is a version behind.** v2 added `overallTemperature`,
   which its `CoffeeProfile` does not have.
4. **It costs two dependencies** — `requests` and `pydantic` — for ten HTTP
   calls and a handful of range checks.

None of that is a security finding against the project. A full scan found no
malware, no obfuscation, no `eval`/`exec`/`subprocess`/`pickle`, no hardcoded
secrets, no disabled TLS verification, and one network destination in the core
library: Fellow's own gateway. The token logging is a debugging default nobody
turned off, and it is a bad fit for this process specifically.

## Tests

```bash
python -m pytest tests/test_aiden.py -q
```

Standard library only, against a fake Fellow that can be made *deaf* — it
accepts a start, returns 200, and leaves the brewer idle forever. That is the
failure you cannot stage against real hardware and the one most worth catching.
The other quiet ones covered: Sunday-first day indexing, an unknown brew state
treated as "not brewing", and a batch basket with no carafe under it.
