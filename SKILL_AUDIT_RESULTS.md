# Skill Audit Results — 2026-08-28

Full audit of every `SKILL.md` in this repo against `SKILL_AUDIT_PROMPT.md` /
`SKILL_AUTHORING_GUIDE.md`. 25 skills total; 1 (`home-assistant-covers`) has
already been rewritten as the worked example, the other 24 were audited only.

## Top-line numbers

- **Worked example:** `home-assistant-covers` went from 423 lines of hot path
  to 149 (+ two `references/*.md` files holding everything that was moved,
  nothing deleted). That's the target shape for every row below marked
  `Needs split`.
- **3 confirmed cross-skill conflicts** — routing hazards, not just style
  issues. These matter more than any single skill's verbosity, because the
  router can silently pick the wrong (stale/contradicting) skill regardless
  of how well either one is written.
- **9 skills need a `references/` split**, 8 need a lighter trim, 6 are
  already fine, 1 needs a rewrite (retire, not edit).

## Cross-skill conflicts (fix these first)

| Conflict | Problem | Fix |
|---|---|---|
| `skills-from-hermes/smart-home/home-assistant` vs. `home-assistant-covers` | The former calls `ha_call_service`/`ha_list_entities`/`ha_get_state` — tool names that don't exist in this deployment's verified 18-tool `mcp__ha__*` inventory. Neither `description` warns the router off the other. | Retire `home-assistant` (or rewrite it to call the real `mcp__ha__*` tools) rather than edit it in place. |
| `skills-from-hermes/homeserver-infrastructure/homeserver-infrastructure` vs. `hermes-local-topology` | Direct factual contradiction: one says each session gets its own container, the other (deployment-verified) says Hermes reuses **one long-lived container**. A model consulting the wrong one gives backwards operational advice. | Delete the generic claim from `homeserver-infrastructure`; point it at `hermes-local-topology` as the source of truth for this deployment. |
| `skills/plex-media-playback` vs. `skills-from-hermes/media/plex-media-playback` | Same name, near-identical description, same domain (play/pause/stop via the `plex` MCP). Both are plausible router hits for the same request. | Pick one as canonical, retire the other. |
| `skills/home-control` vs. `home-assistant-covers` (softer) | Both claim lights/blinds/thermostats for this house; they disagree on whether automations are in scope (`home-control` explicitly says "you are not the control loop," `home-assistant-covers` owns automation authoring). | Scope `home-control`'s description to exclude automations explicitly, or merge. |
| `skills-from-hermes/smart-home/openhue` vs. `home-assistant-covers` (softer) | `openhue` already defers to HA in its body, but the deferral isn't in its `description`, so a description-only router can't see it's a fallback. | Add the deferral to `description`. |

## Full results

| Skill | Body lines | Verdict | Top issue | Action |
|---|---|---|---|---|
| smart-home/home-assistant-covers | 423 → 149 | **Done** | — | Split completed this session; see `references/` |
| smart-home/home-assistant | ~80 | **Needs rewrite** | Calls tools that don't exist in this deployment | Retire or rewrite against `mcp__ha__*` |
| qmd | ~429 | Needs split | Full install/daemon-config/internals inline; usage examples repeated 4x | Split — see below |
| homeserver-infrastructure/hermes-local-topology | ~306 | Needs split | Pervasive "verified/earlier version was wrong" narrative and full incident retellings | Split — see below |
| media/plex-library-curator | ~325 | Needs split | `<details>` block kept "for history"; raw scratch test-log sitting in `references/` | Split — see below |
| media/plex-media-playback (hermes) | ~242 | Needs split | Main body duplicates its own `references/troubleshooting.md` almost verbatim | Dedupe + split |
| media/plex-acquire-movie | ~227 | Needs split | Root-cause incident stories narrated inline, then re-narrated again in Pitfalls | Split — see below |
| skills/household-state | 275 | Needs split | Three full sub-workflows (cooking, phone-capture, time-estimates) narrated in-line; core safety table buried past ~200 lines | Split — see below |
| skills/home-control | 177 | Needs split | One-time device-setup workflow sitting in the hot path; scope conflict with `home-assistant-covers` | Split + fix conflict |
| skills/media-acquisition | 191 | Needs split | Justification narratives for two rules that should just be flat rules | Split — see below |
| media/heartmula | ~161 | Needs split | Full install/venv/patch walkthrough with pasted code diffs before any usage example | Split — see below |
| skills/training-data-curation | 174 | Needs split | Out-of-scope fine-tuning pointer + full JSON template + advanced technique inline with the basic flow | Split — see below |
| smart-home/openhue | ~119 | Trim | Deprecation status not visible in `description`; rationale bullets longer than needed | Trim + fix conflict |
| homeserver-infrastructure/cron-job-operations | ~83 | Trim | Each rule prefixed by a "verified on {date}" discovery story | Cut narrative, keep the rule lines |
| skills/daily-brief | 109 | Trim | Opens with philosophical framing before the tool table; worked example longer than needed | Trim opening + example |
| skills/meal-planning | 93 | Trim | "Learning what they actually eat" (infrequent case) inline with the common path | Tighten to 3-4 lines |
| skills/plex-media-playback | 187 | Trim | Repeats a paragraph already in its own `references/troubleshooting.md` | One-line pointer instead |
| media/youtube-content | ~71 | Trim | `references/output-formats.md` duplicates content already inline in `SKILL.md` | Dedupe (keep one copy) |
| media/gif-search | ~77 | Good | Minor: Setup and Prerequisites sections overlap | Optional merge |
| skills/email-triage | 84 | Good | Marginally over guideline; one paragraph is justification prose | Optional 1-line compress |
| skills/training-calendar-sync | 92 | Good | Marginally over guideline but the "narrative" is a safety-relevant explanation | None required |
| skills/walmart-shopping | 116 | Good | Legitimately multi-step (8 tools); no bloat found | None |
| media/songsee | ~69 | Good | All-tables already, no narrative | None |
| skills/nightly-audit | 73 | Good | Concise, numbered, safety rule stated flat | None |
| homeserver-infrastructure/homeserver-infrastructure | ~60 | Trim | Contradicts `hermes-local-topology` (see conflicts) | Fix contradiction |

## Concrete section moves (for every "Needs split" row)

**hermes-local-topology** → `references/config-quoting-history.md` (the
cmd.exe quoting saga + superseded-claim correction, keep only the two correct
command blocks); `references/incident-log.md` (leaked-credentials story,
wedged-container story, toolset-vanishing story — replace each with its one
flat rule in the hot path).

**plex-library-curator** → delete or relocate
`references/2026-08-15-test-run.md` (it's a scratch log, not documentation);
`references/incident-log.md` for the `<details>` "kept for history" block and
the 2026-08-14 subagent-failure narrative (keep only: "cap delegated census
tasks at ~15 tool calls with a stated scope").

**plex-media-playback (hermes)** → delete the Fire TV paragraph duplicated
from `references/troubleshooting.md`, replace with a pointer;
`references/room-aliases.md` for the machine-identifier table.

**plex-acquire-movie** → `references/incident-log.md` for both root-cause
narratives already summarized as rules in Procedure step 2.

**household-state** → `references/cooking-and-recipes.md`,
`references/phone-capture.md`, `references/task-time-estimates.md`; keep the
tool table, `actor=` attribution rule, shopping-vs-pantry distinction, and
"Correcting a mistake" table in the hot path (highest frequency, highest
error risk — these were buried past 200 lines, move them up as well as
keeping them).

**home-control** → `references/adding-devices.md` for the one-time
`discover_entities` setup workflow; collapse the twice-repeated
locks/alarm-refusal explanation into one line.

**media-acquisition** → cut (not just move) the indexer-bot-check and
Discord-bot-DM justification paragraphs to one flat rule each; move "When
someone asks for something new" to `references/edge-cases.md`.

**heartmula** → `references/installation.md` for the entire
install/venv/dependency-patch walkthrough and code diffs; keep Hardware
Requirements, Usage, Input Formatting, Key Parameters, Pitfalls.

**training-data-curation** → `references/fine-tuning-tools.md` (Unsloth
pointer, explicitly out of scope already); `references/format-and-augmentation.md`
(ShareGPT JSON template + rejection-sampling section); keep filter/dedupe/
write-jsonl as the hot path.

**qmd** → `references/installation.md` (Node/SQLite/npm-or-bun install);
`references/daemon-setup.md` (launchd plist + systemd unit, in full);
`references/internals.md` (RRF/reranking/chunking pipeline details); collapse
the four repeated copies of the usage examples (Quick Reference, Search
Patterns, CLI Usage, Best Practices) into the one Quick Reference table.

## Suggested order of operations

1. Fix the 3 hard conflicts above — they're correctness bugs, cost is low
   (mostly deletion/description edits), and they're the ones that make an
   agent confidently do the *wrong* thing rather than just the *slow* thing.
2. Split `hermes-local-topology` and `qmd` next — biggest, most-loaded
   (topology is `related_skills` for several others), highest narrative
   density.
3. Work down the rest of the "Needs split" list as time allows; the "Trim"
   and "Good" rows are low urgency.
