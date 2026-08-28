# Skill Authoring Guide — Optimizing for Fast, Confident Tool Calls

## The problem this solves

A skill has two readers, at two different moments:

1. **The router**, deciding whether to load the skill at all — reads only `name`,
   `description`, and `tags` in the frontmatter.
2. **The acting model**, which has already loaded the full body and now has to
   decide what to actually do — reads everything else.

Every sentence in the body is something the acting model has to read and reason
about before it commits to a tool call. On a small/fast local model (the kind
Hermes runs against), that reasoning is visible latency: the user is standing
in the room asking for lights before the model finishes deliberating over a
debugging anecdote from three weeks ago.

`home-assistant-covers/SKILL.md` is the reference case: 423 lines, and the
overwhelming majority of it is *why we know what we know* — verification
timestamps, corrected mistakes from earlier sessions, a full narrative of how
the polarity inversion was discovered — rather than *what to do next*. A
request as simple as "turn on the hall light" forces the model through all of
it before it reaches the one line that matters for that call.

## The two-tier model

Split every skill into:

**Hot path — the `SKILL.md` body itself.** Loaded every time the skill is
loaded, read in full before any tool call. Contains only what changes the next
tool call:
- A tool table (name → one-line purpose), not prose.
- The workflow as a short numbered list (discover → act → verify), ideally
  ≤5 steps.
- Non-negotiable correctness/safety facts, stated as flat rules with the
  actionable mapping already worked out — not the story of how they were
  found.
- The single most common call, written out as a literal example, so the model
  can pattern-match instead of deriving syntax from a description.

**Reference (`references/*.md`) — loaded only on demand.** Everything that
doesn't change the next tool call for the common case:
- Verification logs, dates, "confirmed 2026-08-10" provenance.
- Historical corrections ("an earlier version of this skill wrongly claimed…").
- Troubleshooting trees for failure branches.
- Setup/install/config detail a routine action never touches.
- Deep protocol or API detail beyond what the common call needs.

**The test:** if deleting a sentence wouldn't change what the model does on
its next tool call, it doesn't belong in the hot path. Move it to reference or
cut it.

This mirrors a pattern already used elsewhere in this repo —
`plex-media-playback/references/troubleshooting.md`,
`home-assistant/references/polarity-testing.md`,
`youtube-content/references/output-formats.md` — it just isn't applied
consistently yet.

## Writing rules for the hot path

- **Tables over paragraphs** wherever there are more than two parallel facts
  (tool lists, pitfalls, units, error meanings).
- **Rules, not narratives.** "Cover position is inverted here: 100=closed,
  0=open. Always use `set_position`." — not three paragraphs about which
  curtain was eyeballed on which date.
- **No changelog-in-the-hot-path.** "Verified 2026-08-10," "an earlier version
  said X, that was wrong," "this cost a full debugging session" — all
  reference material. It's useful once, to the person maintaining the skill;
  it's a tax on every single invocation after that.
- **Flat sequences, not conditional trees**, for the common case. Branch logic
  ("if the MCP is down, fall back to curl") belongs after the happy path, or
  in reference, not interleaved with it.
- **Order for early exit.** Put the tool call before the caveats. A model
  reading top-to-bottom starts committing to a plan as it reads; caveats that
  arrive *after* the concrete action are corrections to a plan already in
  motion, caveats that arrive *before* it are ambiguity to resolve first. For
  a single-action skill, consider a one-line "quick reference" directly under
  the H1 — the exact call shape for the 90% case — so a model that already
  knows the domain can stop reading immediately.
- **Say what's out of scope, briefly, once.** ("If it needs a lock, alarm, or
  raw service call: not available, don't look for a workaround.") One line,
  not a essay per excluded domain.

## One skill per domain

Two skills currently both claim Home Assistant cover/light control:
`home-assistant` (91 lines, calls `ha_call_service`/`ha_list_entities`, tools
that `home-assistant-covers` explicitly documents as **never having existed**)
and `home-assistant-covers` (the real, MCP-backed one). If the router loads
the stale one, the model will confidently call a tool that doesn't exist, or
worse, act on wrong polarity guidance. When a skill is superseded, delete or
explicitly deprecate it — don't leave two versions of the truth loadable at
once.

## Size targets

- Single-action skills (control a device, look up a fact): **under ~80 lines**
  of hot path.
- Multi-capability skills (one MCP server, many tools): can run longer, but
  should be dominated by tables; anything narrative pushes to `references/`.
- **Any skill over ~150 lines is a signal** it needs a references/ split, not
  a signal that the domain is just "complicated."

## Beyond the skill file: the other lever

Everything above reduces how much the model has to read once it has decided
to think. It does not touch *whether* the model enters extended deliberation
at all — and for a hybrid-reasoning model (Qwen3 included), that's a separate
switch, not a function of prompt length. A perfectly terse skill still gets
read by a model running in thinking mode, which will reason step-by-step and
narrate a plan regardless of how little there is to plan.

Qwen3's hybrid mode can be forced off per request (Ollama exposes this as a
`think` request parameter, and interactively as `/set nothink`; the same
control exists at the chat-template level as `enable_thinking`). If Hermes's
provider config surfaces this knob, the highest-leverage fix for "turn on the
lights" latency is routing routine, single-tool-call intents (device control,
simple lookups) through a no-think profile — not more skill editing. Skill
trimming still matters because it's what's actually loadable/editable from
this repo today, and it helps regardless of which mode the model runs in;
but if the model is stuck thinking through every request regardless of skill
content, that's the inference-level knob to check first, and skills that
legitimately need multi-step judgment (e.g. authoring an automation with the
cover-polarity gotcha) are exactly the cases that should keep thinking mode
on. This is worth a direct check against Hermes's current docs/config rather
than assuming either way.

## Author checklist

- [ ] `description` alone is enough for the router to decide relevance —
      doesn't require reading the body.
- [ ] Tool table exists and is the first thing after "When to Use."
- [ ] Workflow is ≤5 numbered steps for the common case.
- [ ] Every safety/correctness fact is one rule line with the actionable
      mapping, not a discovery narrative.
- [ ] No dates, "verified," or "earlier version said" text in the hot path.
- [ ] Troubleshooting/setup/history lives in `references/`, linked with one
      pointer line ("if X fails, see references/troubleshooting.md").
- [ ] No other loadable skill claims the same tool surface with conflicting
      information.
- [ ] Hot path is under the size target for its category, or has been split.
