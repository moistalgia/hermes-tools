# Skill Audit Prompt

Reusable prompt for auditing any `SKILL.md` (or a batch of them) against this
repo's hot-path/reference split. Paste as-is, filling in the target path(s).
Full rationale for the rubric lives in `SKILL_AUTHORING_GUIDE.md` — this
prompt embeds the checklist so it also works standalone (e.g. pasted into a
fresh session, or run against a skill outside this repo).

---

You are auditing one or more Claude/Hermes `SKILL.md` files for whether their
verbosity would slow down or confuse a fast, low-reasoning-effort model at the
moment it needs to make a tool call. You are **not** optimizing for human
readability or thoroughness — you are optimizing for "how little does the
model have to read and reason about before it can correctly call a tool."

**Audit only. Do not rewrite anything unless explicitly told to `--fix`.**

For each `SKILL.md`, read the full file (and its `references/` dir if any),
then score it against this checklist:

1. **Router clarity** — does `description` alone (no body read) let a router
   decide relevance? Does it overlap or conflict with another skill's claimed
   domain (same tools, same entities, contradicting facts)?
2. **Hot-path purity** — does every paragraph in the body change the model's
   next tool call? Flag any of:
   - Verification timestamps / "confirmed on {date}" provenance
   - "An earlier version of this skill said X, that was wrong" corrections
   - Full debugging narratives ("this cost a full session," discovery stories)
   - Setup/install/config detail unrelated to routine use
   - Troubleshooting trees for failure branches, interleaved with the happy path
3. **Structure** — tool listing as a table (not prose)? Workflow as a short
   numbered list (≤5 steps for the common case)? Safety/correctness facts as
   flat one-line rules with the actionable mapping already resolved, rather
   than narrated?
4. **Ordering** — is the concrete tool call / call shape presented before the
   caveats, so a model reading top-to-bottom can commit to a plan early? Is
   there a one-line "quick reference" for the 90% case near the top?
5. **Size** — line count of the body. Single-action skill over ~80 lines, or
   any skill over ~150 lines, is a split signal by default (call it out even
   if the content itself seems justified — that's a hypothesis for the human
   to confirm, not an automatic verdict).
6. **Reference hygiene** — if a `references/` dir already exists, does the
   hot path actually stay out of it, or is duplicate content in both places?

### Output format

One row per skill in a table:

| Skill | Body lines | Verdict | Top issues (max 3) | Recommended action |
|---|---|---|---|---|

Verdict is one of: `Good` (no action needed), `Trim` (cut narrative in place,
no restructure needed), `Needs split` (move identified sections to
`references/*.md`), `Needs rewrite` (structural problems beyond a simple
split — e.g. conflicts with another skill, missing tool table, buried safety
facts).

For every `Needs split` or `Needs rewrite` skill, follow the table with a
short bullet list naming exactly which sections/paragraphs to move or cut,
and to what reference filename — concrete enough that someone could execute
it without re-reading the whole skill.

Flag cross-skill conflicts (same tool surface, contradicting facts, stale
tool names) as their own callout, not buried in one skill's row.

---

## Batch mode

To run this over every skill in a repo: enumerate all `SKILL.md` files (in
this repo: `skills/*/SKILL.md` and `skills-from-hermes/**/SKILL.md`), apply
the checklist to each, and produce one consolidated report — the per-skill
table plus a short "top offenders" summary (worst 3–5 by verdict/size) and
any cross-skill conflicts found.
