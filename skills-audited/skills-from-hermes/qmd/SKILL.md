---
name: qmd
description: Hybrid local search over notes, docs, and transcripts.
version: 1.0.0
author: Hermes Agent + Teknium
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Search, Knowledge-Base, RAG, Notes, MCP, Local-AI]
    related_skills: [obsidian, hermes-agent, arxiv]
---

# QMD — Query Markup Documents

Local, on-device hybrid search (BM25 + vector + LLM reranking) over markdown
notes, docs, and transcripts. No cloud dependencies. Created by
[Tobi Lütke](https://github.com/tobi/qmd), MIT licensed.

## When to Use

- "Search my notes/docs/knowledge base", "find in my meeting transcripts"
- Semantic search ("find notes about X concept"), not just keyword grep
- Setting up a new local knowledge base or collection

## Quick Reference

| Command | What It Does | Example | Speed |
|---|---|---|---|
| `qmd search "q"` | BM25 keyword search, no models | `qmd search "handleError async"` | ~0.2s |
| `qmd vsearch "q"` | Semantic vector search | `qmd vsearch "ideas for improving onboarding"` | ~3s |
| `qmd query "q"` | Hybrid + reranking (best quality) | `qmd query "what was decided about the API redesign"` | ~2-3s warm, ~19s cold |
| `qmd get <id>` | Retrieve full document | `qmd get "#abc123"` or `qmd get "file.md:50" -l 100` | instant |
| `qmd multi-get "glob"` | Retrieve multiple files | `qmd multi-get "journals/*.md" --json` | instant |
| `qmd collection add <path> --name <n>` | Register a directory | `qmd collection add ~/notes --name notes` | instant |
| `qmd context add <uri> "desc"` | Add retrieval-boosting metadata | `qmd context add qmd://notes "Personal journal entries"` | instant |
| `qmd embed` | (Re)generate embeddings | `qmd embed` | varies |
| `qmd status` | Index health, collections, models | `qmd status` | instant |
| `qmd mcp` | Start MCP server (stdio) | `qmd mcp` | persistent |
| `qmd mcp --http --daemon` | Start MCP server, HTTP, warm models | `qmd mcp --http --daemon` (port 8181) | persistent |

Use `qmd search` for exact terms/identifiers/names (fast, no models). Use
`qmd query` when the question is conceptual or quality matters most.

**Query modifiers:**

| Syntax | Effect |
|---|---|
| `term` | Prefix match (`perf` matches "performance") |
| `"phrase"` | Exact phrase match |
| `-term` | Exclude term |
| `--collection <n>` | Scope to one collection |
| `--json` / `--limit N` | JSON output / cap result count |

For a single query mixing modes, pass newline-separated directives:
`qmd query $'lex: rate limiter\nvec: how does throttling work under load'`
(also supports `expand:` for query expansion, `hyde:` for a hypothetical-answer
embedding). The first line gets 2x weight in fusion — put your most important
query first.

## MCP Integration (preferred — use this over raw CLI when available)

Once `qmd` is registered as an MCP server, these tools are available directly
and this skill doesn't need to be loaded to use them:

| MCP Tool | CLI Equivalent | Description |
|---|---|---|
| `mcp_qmd_search` | `qmd search` | BM25 keyword search |
| `mcp_qmd_vsearch` | `qmd vsearch` | Semantic vector search |
| `mcp_qmd_deep_search` | `qmd query` | Hybrid search + reranking |
| `mcp_qmd_get` | `qmd get` | Retrieve document by ID or path |
| `mcp_qmd_status` | `qmd status` | Index health and stats |

MCP tools accept structured multi-mode queries as JSON:

```json
{
  "searches": [
    {"type": "lex", "query": "authentication middleware"},
    {"type": "vec", "query": "how user login is verified"}
  ],
  "collections": ["project-docs"],
  "limit": 10
}
```

If MCP isn't configured, run the CLI commands above via `terminal` instead —
e.g. `terminal(command="qmd query 'what was decided about the API redesign' --json", timeout=30)`.
For setup/config (registering the server, stdio vs. HTTP daemon mode, keeping
a daemon warm across restarts), see `references/daemon-setup.md`.

## First-Time Setup

1. `qmd collection add <path> --name <name>` — point at a directory of docs.
2. `qmd context add qmd://<name> "<description>"` — **always do this**; it
   materially improves retrieval quality.
3. `qmd embed` — generate embeddings. Re-run after adding new documents.
4. `qmd status` — verify index health before relying on search.

Full install/prerequisites (Node.js version, SQLite on macOS, model
downloads): `references/installation.md`.

## Out of Scope / Troubleshooting

Cold starts, missing collections, extension-load errors, CJK/multilingual
embedding overrides: `references/troubleshooting.md`. Internals of the search
pipeline (query expansion, RRF fusion, chunking): `references/internals.md`.
