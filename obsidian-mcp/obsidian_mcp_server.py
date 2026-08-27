#!/usr/bin/env python3
"""
obsidian-mcp - read, write, and search a local Obsidian vault.

The vault is a folder of Markdown files on disk. There is no Obsidian API to
call - the app itself is just a Markdown editor with conventions layered on
top (frontmatter, `[[wikilinks]]`, `#tags`), and this server speaks those
conventions directly against the filesystem. No bot, no plugin, no relay: the
fewer things between "write this down" and it being on disk, the fewer ways an
evening ends with nothing saved.

Three things it does beyond reading and writing files:

  1. **It never leaves the vault.** Every path argument is resolved against
     OBSIDIAN_VAULT_PATH and checked to still be inside it before anything
     touches disk. `../../../Windows/System32/whatever` is refused, not
     sanitized and allowed through - the whole point of a scoped tool is that
     there is nothing to walk it back from later.

  2. **Deleting moves to `.trash/`, never `rm`.** Same folder Obsidian's own
     local trash uses, so a note removed by mistake is one drag away from
     back, not gone. Nothing in this server does an unrecoverable delete.

  3. **It reads back after every write.** `create_note` and `append_note`
     reopen the file and confirm the bytes landed before reporting success -
     the §3 convention from DESIGN.md, applied to a filesystem instead of an
     API that can lie about what it did.

Frontmatter parsing is best-effort and flat on purpose: `key: value`,
`key: [a, b]`, and block lists (`key:` / `- item`). Nested YAML is not
attempted - `read_note` returns what it can parse and leaves the raw text
available regardless, rather than guessing at a structure and getting it
quietly wrong. Writing frontmatter is not a separate tool: `create_note` and
`append_note` write exactly the content they are given, so a note's
frontmatter is just the top of that text, written in the same call as the
rest of it.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python obsidian_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python obsidian_mcp_server.py vault_status
         python obsidian_mcp_server.py list_notes folder=Projects
         python obsidian_mcp_server.py read_note path="Projects/Climbing.md"

Environment:
    OBSIDIAN_VAULT_PATH    Folder the vault lives in. Required, no default -
                            a wrong or missing path should fail loudly, not
                            silently act on the wrong folder.
    OBSIDIAN_DAILY_FOLDER  Vault-relative folder daily notes live in. Default
                            '' (vault root).
    OBSIDIAN_DAILY_FORMAT  strftime format for a daily note's filename.
                            Default '%Y-%m-%d'.

No dependencies. Standard library only.
"""

import difflib
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
DAILY_FOLDER = os.environ.get("OBSIDIAN_DAILY_FOLDER", "").strip().strip("/")
DAILY_FORMAT = os.environ.get("OBSIDIAN_DAILY_FORMAT", "").strip() or "%Y-%m-%d"


# ---------------------------------------------------------------------------
# Vault path safety
# ---------------------------------------------------------------------------


def vault_root():
    if not VAULT_PATH:
        raise ToolError(
            "OBSIDIAN_VAULT_PATH is not set, so there is no vault to act on. "
            "Set it to the vault's folder in the obsidian entry of "
            "config.yaml. This is final until it is set."
        )
    if not os.path.isdir(VAULT_PATH):
        raise ToolError(
            f"OBSIDIAN_VAULT_PATH is set to {VAULT_PATH!r} but that is not a "
            f"folder that exists. Fix the path in config.yaml - do not guess "
            f"at a different one."
        )
    return VAULT_PATH


def _inside_vault(abs_path, root):
    root_norm = os.path.normcase(os.path.normpath(root))
    path_norm = os.path.normcase(os.path.normpath(abs_path))
    return path_norm == root_norm or path_norm.startswith(root_norm + os.sep)


def resolve_note(rel, must_exist=True):
    """Resolve a vault-relative note path to (abs_path, normalized_rel).

    must_exist=True  -> raise if it does not exist (with suggestions).
    must_exist=False -> raise if it already exists (create_note's guard).
    must_exist=None  -> no existence check either way.
    """
    root = vault_root()
    raw = (rel or "").strip().replace("\\", "/").strip("/")
    if not raw:
        raise ToolError("A note path is required.")
    if not raw.lower().endswith(".md"):
        raw += ".md"
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ToolError(f"{rel!r} tries to leave the vault. Use a path inside it.")

    abs_path = os.path.normpath(os.path.join(root, *parts))
    if not _inside_vault(abs_path, root):
        raise ToolError(f"{rel!r} tries to leave the vault. Use a path inside it.")
    norm_rel = "/".join(parts)

    if must_exist is True and not os.path.isfile(abs_path):
        stem = os.path.splitext(parts[-1])[0]
        similar = find_similar(stem, root)
        extra = {"did_you_mean": similar} if similar else {}
        raise ToolError(
            f"No note at {norm_rel!r}."
            + (f" Did you mean: {', '.join(similar)}?" if similar
               else " Use list_notes or search_notes to find the right path."),
            **extra,
        )
    if must_exist is False and os.path.exists(abs_path):
        raise ToolError(
            f"{norm_rel!r} already exists. Pass overwrite=true to replace it, "
            f"or use append_note to add to it instead."
        )
    return abs_path, norm_rel


def resolve_folder(rel):
    root = vault_root()
    raw = (rel or "").strip().replace("\\", "/").strip("/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError(f"{rel!r} tries to leave the vault. Use a path inside it.")

    abs_path = os.path.normpath(os.path.join(root, *parts)) if parts else root
    if not _inside_vault(abs_path, root):
        raise ToolError(f"{rel!r} tries to leave the vault. Use a path inside it.")
    norm_rel = "/".join(parts)
    if not os.path.isdir(abs_path):
        raise ToolError(
            f"No folder {norm_rel!r} in the vault." if norm_rel
            else "The vault root itself could not be opened."
        )
    return abs_path, norm_rel


# ---------------------------------------------------------------------------
# Walking the vault
# ---------------------------------------------------------------------------


def iter_md_files(folder_abs, recursive=True):
    """Every .md file under folder_abs, skipping dotfolders (.obsidian, .trash,
    .git) - plugin config and vault trash are never notes."""
    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder_abs):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.lower().endswith(".md"):
                    yield os.path.join(dirpath, fn)
    else:
        for fn in sorted(os.listdir(folder_abs)):
            full = os.path.join(folder_abs, fn)
            if os.path.isfile(full) and fn.lower().endswith(".md"):
                yield full


def find_similar(stem, root, limit=5):
    """Notes whose filename resembles `stem`, for a not-found error's
    did_you_mean. Filters to plausible alternatives only - see DESIGN.md
    §2, naming every note in a large vault teaches nothing."""
    by_stem = {}
    for abs_path in iter_md_files(root, recursive=True):
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        by_stem.setdefault(os.path.splitext(os.path.basename(rel))[0], rel)
    close = difflib.get_close_matches(stem, list(by_stem), n=limit, cutoff=0.5)
    return [by_stem[name] for name in close]


def note_title(abs_path):
    """The first '# Heading' in the note, or its filename if there is none."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return os.path.splitext(os.path.basename(abs_path))[0]
    _, body = split_frontmatter(head)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line:
            break
    return os.path.splitext(os.path.basename(abs_path))[0]


# ---------------------------------------------------------------------------
# Frontmatter - flat and best-effort, never raises
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


def split_frontmatter(text):
    """(raw_frontmatter_or_None, body). Never raises - text with no leading
    '---' block just comes back as (None, text)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    try:
        return float(v)
    except ValueError:
        return v


def parse_frontmatter(raw):
    """Flat keys only: `key: value`, `key: [a, b]`, and block lists (`key:`
    followed by `- item` lines). A nested mapping is left as an empty value
    rather than guessed at - this is a convenience read, never load-bearing,
    so returning something wrong would be worse than returning nothing."""
    if not raw or not raw.strip():
        return {}
    data = {}
    key = None
    for line in raw.splitlines():
        item = re.match(r"^\s*-\s?(.*)$", line)
        if item and key is not None and isinstance(data.get(key), list):
            data[key].append(_scalar(item.group(1)))
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [_scalar(v) for v in inner.split(",")] if inner else []
        else:
            data[key] = _scalar(value)
    return data


# ---------------------------------------------------------------------------
# Wikilinks and tags
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
TAG_RE = re.compile(r"(?<![\w#/])#([A-Za-z][A-Za-z0-9_/-]*)")


def extract_links(text):
    out = []
    for m in LINK_RE.finditer(text):
        target = m.group(1).strip()
        if target and target not in out:
            out.append(target)
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "Confirm the vault is reachable and report its shape: how many notes, how "
    "many top-level folders, and where daily notes are configured. Run this "
    "first when something else fails - a missing or wrong "
    "OBSIDIAN_VAULT_PATH looks exactly like an empty vault otherwise."
)
def vault_status():
    root = vault_root()
    total = 0
    top_folders = set()
    for abs_path in iter_md_files(root, recursive=True):
        total += 1
        rel = os.path.relpath(abs_path, root)
        parts = rel.split(os.sep)
        if len(parts) > 1:
            top_folders.add(parts[0])

    daily_where = f"{DAILY_FOLDER}/" if DAILY_FOLDER else "(vault root)/"
    return {
        "ok": True,
        "summary": (
            f"Vault at {root} reachable: {total} note(s) across "
            f"{len(top_folders)} top-level folder(s). Daily notes -> "
            f"{daily_where}<{DAILY_FORMAT}>.md."
        ),
        "vault_path": root,
        "note_count": total,
        "top_level_folders": sorted(top_folders),
        "daily_folder": DAILY_FOLDER or "(vault root)",
        "daily_format": DAILY_FORMAT,
    }


@tool(
    "List notes under a folder, with title, size, and last-modified time. "
    "Empty folder means the whole vault. The discovery tool for browsing - "
    "use it to see what exists before guessing a path.",
    {
        "folder": s("Vault-relative folder, e.g. 'Projects/Climbing'. Empty "
                    "for the whole vault.", default=""),
        "recursive": b("Include subfolders.", default=True),
        "limit": i("Maximum notes to return.", default=200, minimum=1, maximum=2000),
    },
)
def list_notes(folder="", recursive=True, limit=200):
    abs_folder, rel_folder = resolve_folder(folder)
    root = vault_root()
    rows = []
    for abs_path in iter_md_files(abs_folder, recursive=recursive):
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        stat = os.stat(abs_path)
        rows.append({
            "path": rel,
            "title": note_title(abs_path),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    rows.sort(key=lambda r: r["path"].lower())

    truncated = len(rows) > limit
    where = f"'{rel_folder}'" if rel_folder else "the vault"
    return {
        "ok": True,
        "summary": (
            f"{len(rows)} note(s) in {where}"
            + (f", showing the first {limit}." if truncated else ".")
        ),
        "notes": rows[:limit],
        "total": len(rows),
        "truncated": truncated,
    }


@tool(
    "Read a note's full content. Returns the raw text, the parsed "
    "frontmatter (best-effort - flat keys and simple lists only; anything "
    "nested is left out rather than guessed at), and the body with the "
    "frontmatter block stripped off.",
    {"path": s("Vault-relative path, e.g. 'Projects/Climbing/Paradigm.md'. "
               "The '.md' extension is optional.")},
    ["path"],
)
def read_note(path):
    abs_path, rel = resolve_note(path, must_exist=True)
    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    fm_raw, body = split_frontmatter(raw)
    stat = os.stat(abs_path)
    modified = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    return {
        "ok": True,
        "summary": f"{rel} ({len(raw)} chars, modified {modified}).",
        "path": rel,
        "content": raw,
        "frontmatter": parse_frontmatter(fm_raw),
        "body": body,
        "modified": modified,
    }


@tool(
    "Full-text search across notes. Returns matching lines with note path "
    "and line number, capped at `limit` total matches. Reports how many "
    "notes were searched, so a zero-match result can be told apart from a "
    "vault or folder that could not be read at all.",
    {
        "query": s("Text to search for."),
        "folder": s("Restrict to a vault-relative folder. Empty for the "
                    "whole vault.", default=""),
        "case_sensitive": b("Match case exactly.", default=False),
        "limit": i("Maximum matching lines to return, across all notes.",
                   default=50, minimum=1, maximum=500),
    },
    ["query"],
)
def search_notes(query, folder="", case_sensitive=False, limit=50):
    query = (query or "").strip()
    if not query:
        raise ToolError("query cannot be empty.")
    abs_folder, rel_folder = resolve_folder(folder)
    root = vault_root()
    needle = query if case_sensitive else query.lower()

    files_searched = 0
    matches = []
    for abs_path in iter_md_files(abs_folder, recursive=True):
        files_searched += 1
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        for lineno, line in enumerate(lines, 1):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                matches.append({"path": rel, "line": lineno, "text": line.strip()[:200]})
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    where = f"'{rel_folder}'" if rel_folder else "the vault"
    if not matches:
        return {
            "ok": True,
            "summary": f"No matches for {query!r} in {where} ({files_searched} note(s) searched).",
            "matches": [],
            "files_searched": files_searched,
            "truncated": False,
        }

    truncated = len(matches) >= limit
    notes_hit = len({m["path"] for m in matches})
    return {
        "ok": True,
        "summary": (
            f"{len(matches)} match(es) for {query!r} across {notes_hit} "
            f"note(s) in {where} ({files_searched} searched)."
            + (" Hit the limit - narrow the query or folder for the rest." if truncated else "")
        ),
        "matches": matches,
        "files_searched": files_searched,
        "truncated": truncated,
    }


@tool(
    "Create a new note. Fails if one already exists at that path - pass "
    "overwrite=true to replace it, or use append_note to add to an existing "
    "one instead. Any frontmatter goes at the top of `content` as literal "
    "'---' YAML; this tool writes exactly what it is given and does not "
    "generate or merge frontmatter itself.\n\n"
    "Reads the file back after writing, so the result reflects what is "
    "actually on disk rather than what was requested.",
    {
        "path": s("Vault-relative path for the new note. '.md' is added if omitted."),
        "content": s("Full note content, including any frontmatter block.", default=""),
        "overwrite": b("Replace an existing note at this path instead of failing.", default=False),
    },
    ["path"],
)
def create_note(path, content="", overwrite=False):
    abs_path, rel = resolve_note(path, must_exist=None if overwrite else False)
    existed = os.path.isfile(abs_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        written = fh.read()
    if written != content:
        raise ToolError(
            f"Wrote {rel} but the read-back does not match what was sent. "
            f"Re-read it with read_note before trusting it."
        )
    return {
        "ok": True,
        "summary": f"{'Replaced' if existed else 'Created'} {rel} ({len(content)} chars, confirmed).",
        "path": rel,
        "replaced": existed,
        "size_bytes": len(content.encode("utf-8")),
    }


@tool(
    "Append text to the end of a note, creating it first if it does not "
    "exist (default). A blank line separates the new text from whatever was "
    "already there. Reads the file back afterward to confirm the text "
    "actually landed.",
    {
        "path": s("Vault-relative path. '.md' is added if omitted."),
        "content": s("Text to append."),
        "create_if_missing": b("Create the note if it does not exist yet, "
                               "instead of failing.", default=True),
    },
    ["path", "content"],
)
def append_note(path, content, create_if_missing=True):
    abs_path, rel = resolve_note(path, must_exist=None)
    exists = os.path.isfile(abs_path)
    if not exists and not create_if_missing:
        raise ToolError(
            f"No note at {rel}. Pass create_if_missing=true, or use "
            f"create_note to make it explicitly."
        )

    prefix = ""
    if exists:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
        if existing and not existing.endswith("\n"):
            prefix += "\n"
        if existing.strip():
            prefix += "\n"
    else:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    with open(abs_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(prefix + content)

    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        final = fh.read()
    if not final.endswith(content):
        raise ToolError(
            f"Appended to {rel} but the read-back does not end with the new "
            f"text. Re-read it with read_note before trusting it."
        )

    return {
        "ok": True,
        "summary": f"{'Appended to' if exists else 'Created'} {rel} ({len(content)} chars added, confirmed).",
        "path": rel,
        "created": not exists,
        "total_size_bytes": len(final.encode("utf-8")),
    }


@tool(
    "Get today's daily note, or a specific date's. Creates it if missing "
    "(default), using OBSIDIAN_DAILY_FOLDER and OBSIDIAN_DAILY_FORMAT. Pass "
    "ensure=false to check without creating one.",
    {
        "date": s("Date as YYYY-MM-DD. Empty means today.", default=""),
        "ensure": b("Create the note if it is missing.", default=True),
    },
)
def daily_note(date="", ensure=True):
    date = (date or "").strip()
    if date:
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise ToolError(f"{date!r} is not a date in YYYY-MM-DD form.")
    else:
        d = datetime.now().date()

    filename = d.strftime(DAILY_FORMAT)
    target = f"{DAILY_FOLDER}/{filename}" if DAILY_FOLDER else filename
    abs_path, rel = resolve_note(target, must_exist=None)
    existed = os.path.isfile(abs_path)

    if not existed:
        if not ensure:
            return {
                "ok": True,
                "summary": f"No daily note yet for {d.isoformat()} ({rel}).",
                "path": rel,
                "existed": False,
                "content": None,
            }
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    return {
        "ok": True,
        "summary": f"{'Created' if not existed else 'Found'} daily note for {d.isoformat()} at {rel}.",
        "path": rel,
        "existed": existed,
        "content": content,
    }


@tool(
    "Remove a note. Never deletes for real - it moves the file into "
    "'.trash/' at the vault root, preserving its folder structure, the same "
    "place Obsidian's own local trash uses. Recoverable by moving it back.",
    {"path": s("Vault-relative path of the note to remove.")},
    ["path"],
)
def delete_note(path):
    abs_path, rel = resolve_note(path, must_exist=True)
    root = vault_root()
    dest = os.path.join(root, ".trash", *rel.split("/"))
    if os.path.exists(dest):
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        base, ext = os.path.splitext(dest)
        dest = f"{base}.{stamp}{ext}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(abs_path, dest)

    if os.path.exists(abs_path) or not os.path.isfile(dest):
        raise ToolError(
            f"Tried to move {rel} to .trash/ but the result does not look "
            f"right. Check the vault's .trash folder by hand before "
            f"assuming anything about {rel}."
        )
    return {
        "ok": True,
        "summary": f"Moved {rel} to .trash/ (confirmed, recoverable).",
        "path": rel,
        "trash_path": os.path.relpath(dest, root).replace(os.sep, "/"),
    }


@tool(
    "List every tag used in the vault - frontmatter 'tags:' and inline "
    "'#tag' alike - with how many notes use each, most-used first. The "
    "discovery tool for tags: use this before guessing a tag name.",
    {"limit": i("Maximum tags to return.", default=100, minimum=1, maximum=1000)},
)
def list_tags(limit=100):
    root = vault_root()
    counts = {}
    for abs_path in iter_md_files(root, recursive=True):
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        fm_raw, body = split_frontmatter(raw)
        found = set()

        fm_tags = parse_frontmatter(fm_raw).get("tags")
        if isinstance(fm_tags, list):
            candidates = fm_tags
        elif isinstance(fm_tags, str):
            candidates = re.split(r"[,\s]+", fm_tags)
        else:
            candidates = []
        for t in candidates:
            t = str(t).strip().lstrip("#")
            if t:
                found.add(t)

        for m in TAG_RE.finditer(body):
            t = m.group(1)
            if not t.isdigit():
                found.add(t)

        for t in found:
            counts[t] = counts.get(t, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    truncated = len(ranked) > limit
    return {
        "ok": True,
        "summary": (
            f"{len(ranked)} tag(s) across the vault"
            + (f", showing the top {limit}." if truncated else ".")
        ),
        "tags": [{"tag": t, "notes": n} for t, n in ranked[:limit]],
        "total": len(ranked),
        "truncated": truncated,
    }


@tool(
    "Outgoing [[wikilinks]] from a note, and its backlinks - other notes "
    "that link to it. Obsidian resolves links by filename, not full path, "
    "so backlinks are matched the same way rather than requiring an exact "
    "path match.",
    {"path": s("Vault-relative path of the note.")},
    ["path"],
)
def note_links(path):
    abs_path, rel = resolve_note(path, must_exist=True)
    root = vault_root()
    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        outgoing = extract_links(fh.read())

    stem = os.path.splitext(os.path.basename(rel))[0].lower()
    backlinks = []
    for other_abs in iter_md_files(root, recursive=True):
        if os.path.normcase(other_abs) == os.path.normcase(abs_path):
            continue
        try:
            with open(other_abs, "r", encoding="utf-8", errors="replace") as fh:
                other_raw = fh.read()
        except OSError:
            continue
        for target in extract_links(other_raw):
            target_stem = os.path.splitext(os.path.basename(target.replace("\\", "/")))[0]
            if target_stem.lower() == stem:
                backlinks.append(os.path.relpath(other_abs, root).replace(os.sep, "/"))
                break

    return {
        "ok": True,
        "summary": f"{rel}: {len(outgoing)} outgoing link(s), {len(backlinks)} backlink(s).",
        "path": rel,
        "outgoing": outgoing,
        "backlinks": sorted(backlinks),
    }


def banner():
    return (f"OBSIDIAN_VAULT_PATH={VAULT_PATH or 'MISSING'}  "
            f"OBSIDIAN_DAILY_FOLDER={DAILY_FOLDER or '(vault root)'}  "
            f"OBSIDIAN_DAILY_FORMAT={DAILY_FORMAT}")


if __name__ == "__main__":
    run("obsidian-mcp", "1.0", banner)
