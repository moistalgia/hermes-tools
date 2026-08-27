# obsidian-mcp

Read, write, and search a local Obsidian vault — Markdown files on disk, with
Obsidian's own conventions layered on top: frontmatter, `[[wikilinks]]`,
`#tags`, daily notes.

There is no Obsidian API to call. The app is a Markdown editor with
conventions; this server speaks those conventions directly against the
filesystem. Standard library only. No dependencies, no venv.

## Setup

### 1. Point it at a vault

Any folder Obsidian opens as a vault works — this server does not need
Obsidian running, or even installed. It just needs the folder.

### 2. Hermes

```yaml
  obsidian:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/obsidian-mcp/obsidian_mcp_server.py", "serve"]
    env:
      OBSIDIAN_VAULT_PATH: "C:/Users/you/Documents/Vault"
      OBSIDIAN_DAILY_FOLDER: "Journal"
      OBSIDIAN_DAILY_FORMAT: "%Y-%m-%d"
```

`OBSIDIAN_VAULT_PATH` is **required and has no default** — a wrong or missing
path should fail loudly, not silently act on the wrong folder. `vault_status`
reports it, so a mistake is visible in one call.

`OBSIDIAN_DAILY_FOLDER` (default `''`, the vault root) and
`OBSIDIAN_DAILY_FORMAT` (default `%Y-%m-%d`, a `strftime` pattern) control
where `daily_note` looks. Match whatever the Daily Notes core plugin is
already configured to use, if the vault has one, so both land on the same
file.

### 3. Prove it

```bash
python obsidian-mcp/obsidian_mcp_server.py vault_status
```

Reports whether the vault is reachable, how many notes it holds, and where
daily notes are configured — one call instead of finding out three tools in.

## Tools

| Tool | Does |
| --- | --- |
| `vault_status` | Reachability, note count, top-level folders, daily note config. Run first when something is wrong. |
| `list_folders` | Every folder in the vault, with note counts. Check here before filing something under a new category name. |
| `list_notes` | Browse a folder (or the whole vault): path, title, size, modified. The discovery tool. |
| `read_note` | Full content, plus best-effort parsed frontmatter and the body with it stripped off. |
| `search_notes` | Full-text search, grouped by note, with line numbers. Reports how many notes were searched. |
| `create_note` | Write a new note. Fails if one already exists, unless `overwrite=true`. |
| `append_note` | Add text to the end of a note. Creates it first by default. |
| `daily_note` | Get (or create) today's daily note, or a specific date's. |
| `delete_note` | Move a note to `.trash/`. Never a real delete. |
| `list_tags` | Every tag in the vault — frontmatter and inline — with counts. The discovery tool for tags. |
| `note_links` | A note's outgoing `[[wikilinks]]` and its backlinks. |

Every one is also a CLI subcommand through the same dispatch path:

```bash
python obsidian-mcp/obsidian_mcp_server.py read_note path="Projects/Climbing.md"
python obsidian-mcp/obsidian_mcp_server.py search_notes query="paradigm"
```

## Organizing: a folder is a notebook

There is no separate "notebook" concept and no tool to create one. `create_note`
and `append_note` take any vault-relative path and create whatever parent
folders it needs, so filing something under `Recipes/Pasta.md` or
`Training/2026-08-27.md` for the first time is exactly the same call as any
other write — the folder comes into existence because a note landed in it, the
same way it would in Obsidian's own file explorer.

The only discipline this needs is checking `list_folders` before inventing a
category, so a vault does not end up with `Recipes/` and `Recipe Notes/` as
two different places for the same thing. That is why `list_folders` exists and
why its description says to check it first, rather than this server enforcing
a fixed set of top-level folders the way `state-mcp` enforces a fixed set of
task areas — a notes vault's categories are the user's to invent, and a closed
vocabulary here would fight the way people actually use one.

## The vault jail

Every path argument is resolved against `OBSIDIAN_VAULT_PATH` and checked to
still be inside it before anything touches disk. `../../../Windows/win.ini`,
an absolute path, a drive letter — all refused, not sanitized and let
through. A scoped tool with a path that can walk out of its scope is not a
scoped tool.

`.obsidian/`, `.trash/`, and any other dot-folder are skipped by every
listing, search, and tag scan — plugin configuration and vault trash are
never notes, and showing them as if they were is how an agent ends up
"reading a note" that is actually `workspace.json`.

## Deleting is never `rm`

`delete_note` moves the file into `.trash/` at the vault root, preserving its
folder structure — the same place Obsidian's own local trash uses. Nothing in
this server does an unrecoverable delete; a note removed by mistake is one
move away from back, not gone. A name collision in `.trash/` gets a timestamp
suffix rather than overwriting whatever is already there.

## Frontmatter is read structured, written raw

`read_note` parses frontmatter on a best-effort basis: flat `key: value`
pairs, inline lists (`tags: [a, b]`), and block lists (`key:` / `- item`).
Anything nested comes back absent rather than guessed at wrong — this is a
convenience read, not something anything else here depends on.

There is no `update_frontmatter` tool, on purpose. `create_note` and
`append_note` write exactly the text they are given, so a note's frontmatter
is just the top of `content`, authored the same way as the rest of the note.
Round-tripping a full YAML writer for one server was worse than trusting the
model to write three lines of `---` correctly, and it keeps every write tool
a plain string in, plain string out — the same shape as everything else in
this repo, and CLI-testable the same way.

## Read-back after every write

`create_note` and `append_note` reopen the file after writing and confirm the
content actually landed before reporting success. `delete_note` confirms the
original path is gone and the trash path exists. This is the §3 convention
from [DESIGN.md](../DESIGN.md), applied to a filesystem instead of an API
that can return `200` for a write that silently failed — a full disk, a
permissions error partway through, a sync client (iCloud, OneDrive, Obsidian
Sync) holding the file open at the exact wrong moment.

## Links resolve like Obsidian resolves them

`note_links` extracts `[[wikilinks]]` by filename, not full path — the same
rule Obsidian itself uses when a link doesn't specify a folder. A backlink is
any other note containing a wikilink whose filename matches, so it works
whether the linking note wrote `[[Target]]` or `[[Projects/Target]]`.
