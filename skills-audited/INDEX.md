# Audited Skills — Index

All 25 skills in the repo have been audited against `SKILL_AUTHORING_GUIDE.md`
/ `SKILL_AUDIT_PROMPT.md`. Every row below has a completed copy under this
directory, mirroring its source path exactly. **Originals under `skills/` and
`skills-from-hermes/` were left untouched** — nothing here is live until you
copy/deploy it back (via `skill_manage` for anything sourced from
`skills-from-hermes/`).

| Status | Skill | Source | Audited copy | Lines (before → after) |
|---|---|---|---|---|
| ✅ Done | home-assistant-covers | `skills-from-hermes/smart-home/home-assistant-covers/` | [SKILL.md](skills-from-hermes/smart-home/home-assistant-covers/SKILL.md) | 423 → 149 |
| ✅ Done | qmd | `skills-from-hermes/qmd/` | [SKILL.md](skills-from-hermes/qmd/SKILL.md) | 429 → 107 |
| ✅ Done | hermes-local-topology | `skills-from-hermes/homeserver-infrastructure/hermes-local-topology/` | [SKILL.md](skills-from-hermes/homeserver-infrastructure/hermes-local-topology/SKILL.md) | 318 → 247 |
| ✅ Done | plex-library-curator | `skills-from-hermes/media/plex-library-curator/` | [SKILL.md](skills-from-hermes/media/plex-library-curator/SKILL.md) | 337 → 309 |
| ✅ Done (retired) | home-assistant | `skills-from-hermes/smart-home/home-assistant/` | [SKILL.md](skills-from-hermes/smart-home/home-assistant/SKILL.md) | 91 → 25 (deprecation stub — calls tools that don't exist here; redirects to home-assistant-covers) |
| ✅ Done | household-state | `skills/household-state/` | [SKILL.md](skills/household-state/SKILL.md) | 280 → 163 |
| ✅ Done | home-control | `skills/home-control/` | [SKILL.md](skills/home-control/SKILL.md) | 182 → 155 |
| ✅ Done | media-acquisition | `skills/media-acquisition/` | [SKILL.md](skills/media-acquisition/SKILL.md) | 196 → 186 |
| ✅ Done | training-data-curation | `skills/training-data-curation/` | [SKILL.md](skills/training-data-curation/SKILL.md) | 179 → 132 |
| ✅ Done | plex-media-playback (hermes) | `skills-from-hermes/media/plex-media-playback/` | [SKILL.md](skills-from-hermes/media/plex-media-playback/SKILL.md) | 248 → 146 |
| ✅ Done | plex-acquire-movie | `skills-from-hermes/media/plex-acquire-movie/` | [SKILL.md](skills-from-hermes/media/plex-acquire-movie/SKILL.md) | 238 → 167 |
| ✅ Done | heartmula | `skills-from-hermes/media/heartmula/` | [SKILL.md](skills-from-hermes/media/heartmula/SKILL.md) | 171 → 93 |
| ✅ Done | cron-job-operations | `skills-from-hermes/homeserver-infrastructure/cron-job-operations/` | [SKILL.md](skills-from-hermes/homeserver-infrastructure/cron-job-operations/SKILL.md) | 94 → 79 |
| ✅ Done (conflict fixed) | homeserver-infrastructure | `skills-from-hermes/homeserver-infrastructure/homeserver-infrastructure/` | [SKILL.md](skills-from-hermes/homeserver-infrastructure/homeserver-infrastructure/SKILL.md) | 72 → 79 (net growth: replaced a wrong claim with a correct pointer) |
| ✅ Done (conflict flagged) | openhue | `skills-from-hermes/smart-home/openhue/` | [SKILL.md](skills-from-hermes/smart-home/openhue/SKILL.md) | 133 → 126 (deferral now visible in `description`) |
| ✅ Done | gif-search | `skills-from-hermes/media/gif-search/` | [SKILL.md](skills-from-hermes/media/gif-search/SKILL.md) | 91 → 85 |
| ✅ Done (no change) | songsee | `skills-from-hermes/media/songsee/` | [SKILL.md](skills-from-hermes/media/songsee/SKILL.md) | 83 → 83 |
| ✅ Done | youtube-content | `skills-from-hermes/media/youtube-content/` | [SKILL.md](skills-from-hermes/media/youtube-content/SKILL.md) | 83 → 77 |
| ✅ Done | daily-brief | `skills/daily-brief/` | [SKILL.md](skills/daily-brief/SKILL.md) | 114 → 106 |
| ✅ Done | email-triage | `skills/email-triage/` | [SKILL.md](skills/email-triage/SKILL.md) | 89 → 86 |
| ✅ Done | meal-planning | `skills/meal-planning/` | [SKILL.md](skills/meal-planning/SKILL.md) | 98 → 95 |
| ✅ Done (no change) | nightly-audit | `skills/nightly-audit/` | [SKILL.md](skills/nightly-audit/SKILL.md) | 78 → 78 |
| ✅ Done (conflict flagged) | plex-media-playback (skills/) | `skills/plex-media-playback/` | [SKILL.md](skills/plex-media-playback/SKILL.md) | 192 → 192 (duplication comment added, one paragraph trimmed to a pointer) |
| ✅ Done (no change) | training-calendar-sync | `skills/training-calendar-sync/` | [SKILL.md](skills/training-calendar-sync/SKILL.md) | 97 → 97 |
| ✅ Done (no change) | walmart-shopping | `skills/walmart-shopping/` | [SKILL.md](skills/walmart-shopping/SKILL.md) | 121 → 121 |

All 25 skills are now done.

## Still-open cross-skill conflicts (need a decision, not just an edit)

- **`skills/plex-media-playback` vs. `skills-from-hermes/media/plex-media-playback`** — same domain, same MCP, both plausible router hits. Both audited copies exist; pick one to keep and retire the other.
- **`skills/home-control` vs. `home-assistant-covers`** — automation-boundary line added to `home-control`'s audited copy, but both skills still independently exist for lights/blinds/thermostats. Worth confirming the boundary line is sufficient or whether one should fully own the domain.

## Everything else

Full rationale: [`../SKILL_AUTHORING_GUIDE.md`](../SKILL_AUTHORING_GUIDE.md).
Full findings this index summarizes: [`../SKILL_AUDIT_RESULTS.md`](../SKILL_AUDIT_RESULTS.md).
