# Canon list cache

One file per source, named `<source>.md`, e.g. `afi-100.md`, `sight-and-sound-critics.md`,
`tspdt-1000.md`, `letterboxd-top-250.md`. Each file holds just `Title (Year)`,
one per line — nothing else. Not the fetched page, not commentary, not ranking
notes: those cost tokens to re-read on every future run for zero benefit once
the diff-against-census step is done.

## Format

```
Title One (1974)
Title Two (1999)
Title Three (2003)
```

## Staleness rule

Before fetching a source live, check this file's mtime. Under ~90 days old:
read it and skip the web fetch. Missing or older: `web_search`/`web_extract`
the source, reduce the result to the format above, and overwrite this file.

These lists don't move week to week — AFI 100 has been stable for decades,
Sight & Sound refreshes on a ~10-year cycle. A 90-day cache window costs
nothing in staleness and saves a full research pass (10-30K tokens per source)
on every recurring curation run after the first.

## Why this exists

See the "Batch / continuous curation" section of `../../SKILL.md` — this
cache is what turns canon-source research from a per-run cost into a
per-quarter one.
