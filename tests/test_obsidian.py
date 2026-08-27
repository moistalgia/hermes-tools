"""obsidian-mcp: the vault jail, read-back after every write, and the two
places a filesystem-backed server can return a confident wrong answer.

Everything here can fail quietly:

  * a path that escapes the vault instead of being refused - the one bug in
    this server that would matter beyond an annoying error message,
  * `search_notes` returning zero matches for a broken folder the same way it
    returns zero for a query nobody used, which sends an agent hunting for a
    better query against a vault it never actually searched,
  * frontmatter parsed into something plausible but wrong, which is why the
    parser is tested against exactly the shapes Obsidian actually writes
    rather than assumed to work from reading the regex.

No dependencies. tempfile gives every test its own throwaway vault so writes,
deletes, and the trash folder never leak between tests.
"""

import os
import shutil
import tempfile
import unittest

from support import load

obsidian = load("obsidian_under_test", "obsidian-mcp/obsidian_mcp_server.py",
                 env={"OBSIDIAN_VAULT_PATH": None,
                      "OBSIDIAN_DAILY_FOLDER": None,
                      "OBSIDIAN_DAILY_FORMAT": None})


class VaultCase(unittest.TestCase):
    """Each test gets an empty vault directory of its own."""

    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="obsidian-mcp-test-")
        obsidian.VAULT_PATH = self.vault
        obsidian.DAILY_FOLDER = ""
        obsidian.DAILY_FORMAT = "%Y-%m-%d"

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def write(self, rel, content):
        path = os.path.join(self.vault, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path


class TestVaultConfig(unittest.TestCase):
    """The two ways a server never gets to the filesystem at all."""

    def test_unset_vault_path_is_named_not_treated_as_empty(self):
        obsidian.VAULT_PATH = ""
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.vault_status()
        self.assertIn("OBSIDIAN_VAULT_PATH is not set", str(cm.exception))

    def test_a_vault_path_that_does_not_exist_is_named_not_silently_empty(self):
        obsidian.VAULT_PATH = os.path.join(tempfile.gettempdir(), "definitely-not-a-real-vault-xyz")
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.vault_status()
        self.assertIn("not a folder that exists", str(cm.exception))


class TestVaultJail(VaultCase):
    """The one bug here that would matter beyond a bad error message."""

    def test_dotdot_is_refused_not_sanitized(self):
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.read_note(path="../../../../etc/passwd")
        self.assertIn("leave the vault", str(cm.exception))

    def test_dotdot_inside_a_deeper_path_is_also_refused(self):
        with self.assertRaises(obsidian.ToolError):
            obsidian.create_note(path="Projects/../../outside.md", content="x")

    def test_a_windows_absolute_path_does_not_escape_either(self):
        with self.assertRaises(obsidian.ToolError):
            obsidian.read_note(path="C:/Windows/win.ini")

    def test_folder_traversal_is_refused_the_same_way(self):
        with self.assertRaises(obsidian.ToolError):
            obsidian.list_notes(folder="../")

    def test_an_ordinary_nested_path_is_allowed(self):
        self.write("Projects/Climbing/Plan.md", "# Plan")
        result = obsidian.read_note(path="Projects/Climbing/Plan.md")
        self.assertTrue(result["ok"])


class TestReadWrite(VaultCase):
    """Create, append, and the read-back that catches a silent short write."""

    def test_create_then_read_round_trips_exactly(self):
        obsidian.create_note(path="Note.md", content="hello world")
        result = obsidian.read_note(path="Note.md")
        self.assertEqual(result["content"], "hello world")

    def test_md_extension_is_optional_on_every_path_argument(self):
        obsidian.create_note(path="Note", content="x")
        self.assertTrue(os.path.isfile(os.path.join(self.vault, "Note.md")))
        self.assertEqual(obsidian.read_note(path="Note")["path"], "Note.md")

    def test_create_refuses_to_clobber_by_default(self):
        obsidian.create_note(path="Note.md", content="first")
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.create_note(path="Note.md", content="second")
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual(obsidian.read_note(path="Note.md")["content"], "first")

    def test_overwrite_true_replaces_it(self):
        obsidian.create_note(path="Note.md", content="first")
        obsidian.create_note(path="Note.md", content="second", overwrite=True)
        self.assertEqual(obsidian.read_note(path="Note.md")["content"], "second")

    def test_create_makes_missing_parent_folders(self):
        obsidian.create_note(path="A/B/C/Deep.md", content="x")
        self.assertTrue(os.path.isfile(os.path.join(self.vault, "A", "B", "C", "Deep.md")))

    def test_append_to_missing_note_creates_it(self):
        result = obsidian.append_note(path="New.md", content="first line")
        self.assertTrue(result["created"])
        self.assertEqual(obsidian.read_note(path="New.md")["content"], "first line")

    def test_append_without_create_if_missing_fails_instead_of_silently_creating(self):
        with self.assertRaises(obsidian.ToolError):
            obsidian.append_note(path="New.md", content="x", create_if_missing=False)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "New.md")))

    def test_append_lands_after_existing_content_not_instead_of_it(self):
        obsidian.create_note(path="Log.md", content="line one")
        obsidian.append_note(path="Log.md", content="line two")
        content = obsidian.read_note(path="Log.md")["content"]
        self.assertIn("line one", content)
        self.assertTrue(content.strip().endswith("line two"))

    def test_not_found_offers_a_close_match_not_a_bare_404(self):
        obsidian.create_note(path="Projects/Climbing.md", content="x")
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.read_note(path="Climing")
        self.assertIn("Projects/Climbing.md", cm.exception.extra.get("did_you_mean", []))


class TestFrontmatter(unittest.TestCase):
    """Flat, best-effort, and never raises - tested against the shapes
    Obsidian actually writes, not assumed to work from the regex."""

    def test_no_frontmatter_block_is_not_an_error(self):
        fm_raw, body = obsidian.split_frontmatter("# Just a note\ntext")
        self.assertIsNone(fm_raw)
        self.assertEqual(obsidian.parse_frontmatter(fm_raw), {})

    def test_scalar_keys(self):
        data = obsidian.parse_frontmatter("status: active\npriority: 2\ndone: false\n")
        self.assertEqual(data, {"status": "active", "priority": 2, "done": False})

    def test_inline_list(self):
        data = obsidian.parse_frontmatter("tags: [climbing, training]\n")
        self.assertEqual(data["tags"], ["climbing", "training"])

    def test_block_list(self):
        data = obsidian.parse_frontmatter("tags:\n  - climbing\n  - training\n")
        self.assertEqual(data["tags"], ["climbing", "training"])

    def test_quoted_string_keeps_its_content_not_its_quotes(self):
        data = obsidian.parse_frontmatter('title: "Season 1: Notes"\n')
        self.assertEqual(data["title"], "Season 1: Notes")

    def test_split_frontmatter_separates_block_from_body(self):
        text = "---\nstatus: active\n---\n# Body\ntext\n"
        fm_raw, body = obsidian.split_frontmatter(text)
        self.assertEqual(fm_raw, "status: active")
        self.assertEqual(body, "# Body\ntext\n")


class TestSearch(VaultCase):
    """The empty-result ambiguity DESIGN.md calls out: zero for no matches
    and zero for nothing to search look identical unless the count is
    reported alongside."""

    def test_zero_matches_still_reports_how_much_was_searched(self):
        self.write("A.md", "nothing relevant here")
        self.write("B.md", "nor here")
        result = obsidian.search_notes(query="paradigm")
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["files_searched"], 2)

    def test_an_empty_folder_reports_zero_searched_not_zero_matches(self):
        os.makedirs(os.path.join(self.vault, "Empty"))
        result = obsidian.search_notes(query="anything", folder="Empty")
        self.assertEqual(result["files_searched"], 0)

    def test_case_insensitive_by_default(self):
        self.write("A.md", "Paradigm Training")
        result = obsidian.search_notes(query="paradigm")
        self.assertEqual(len(result["matches"]), 1)

    def test_case_sensitive_when_asked(self):
        self.write("A.md", "Paradigm Training")
        result = obsidian.search_notes(query="paradigm", case_sensitive=True)
        self.assertEqual(result["matches"], [])

    def test_dotfolders_are_never_searched(self):
        self.write(".obsidian/workspace.json", "paradigm")
        self.write(".trash/Old.md", "paradigm")
        result = obsidian.search_notes(query="paradigm")
        self.assertEqual(result["files_searched"], 0)

    def test_limit_truncates_and_says_so(self):
        for n in range(5):
            self.write(f"N{n}.md", "match match match")
        result = obsidian.search_notes(query="match", limit=3)
        self.assertEqual(len(result["matches"]), 3)
        self.assertTrue(result["truncated"])


class TestDeleteIsRecoverable(VaultCase):
    """Never rm - the whole reason this tool exists instead of os.remove."""

    def test_delete_moves_to_trash_not_gone(self):
        obsidian.create_note(path="Note.md", content="x")
        result = obsidian.delete_note(path="Note.md")
        self.assertFalse(os.path.exists(os.path.join(self.vault, "Note.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.vault, ".trash", "Note.md")))
        self.assertEqual(result["trash_path"], ".trash/Note.md")

    def test_a_second_delete_of_a_same_named_note_does_not_clobber_the_first(self):
        obsidian.create_note(path="Note.md", content="first")
        obsidian.delete_note(path="Note.md")
        obsidian.create_note(path="Note.md", content="second")
        obsidian.delete_note(path="Note.md")
        trash = os.listdir(os.path.join(self.vault, ".trash"))
        self.assertEqual(len(trash), 2)

    def test_deleting_something_missing_is_named_not_a_bare_failure(self):
        with self.assertRaises(obsidian.ToolError) as cm:
            obsidian.delete_note(path="Ghost.md")
        self.assertIn("No note at", str(cm.exception))


class TestTagsAndLinks(VaultCase):
    def test_frontmatter_and_inline_tags_both_count(self):
        self.write("A.md", "---\ntags: [climbing]\n---\nbody #health here")
        self.write("B.md", "no frontmatter, just #climbing inline")
        result = obsidian.list_tags()
        by_tag = {row["tag"]: row["notes"] for row in result["tags"]}
        self.assertEqual(by_tag["climbing"], 2)
        self.assertEqual(by_tag["health"], 1)

    def test_a_markdown_heading_is_never_read_as_a_tag(self):
        self.write("A.md", "# Heading One\n## Heading Two\nno tags here")
        result = obsidian.list_tags()
        self.assertEqual(result["tags"], [])

    def test_a_purely_numeric_hashtag_is_not_a_tag(self):
        self.write("A.md", "See issue #42 for details")
        result = obsidian.list_tags()
        self.assertEqual(result["tags"], [])

    def test_outgoing_links_are_extracted_from_wikilinks(self):
        self.write("A.md", "See [[B]] and [[Projects/C|see C]] for more.")
        self.write("B.md", "x")
        self.write("Projects/C.md", "x")
        result = obsidian.note_links(path="A.md")
        self.assertEqual(set(result["outgoing"]), {"B", "Projects/C"})

    def test_backlinks_are_matched_by_filename_not_full_path(self):
        self.write("Projects/Target.md", "x")
        self.write("Other.md", "linking to [[Target]] here")
        result = obsidian.note_links(path="Projects/Target.md")
        self.assertEqual(result["backlinks"], ["Other.md"])

    def test_a_note_never_backlinks_to_itself(self):
        self.write("Self.md", "[[Self]] referencing itself")
        result = obsidian.note_links(path="Self.md")
        self.assertEqual(result["backlinks"], [])


class TestDailyNote(VaultCase):
    def test_ensure_true_creates_it_on_first_call(self):
        result = obsidian.daily_note(date="2026-03-10")
        self.assertFalse(result["existed"])
        self.assertEqual(result["path"], "2026-03-10.md")
        self.assertTrue(os.path.isfile(os.path.join(self.vault, "2026-03-10.md")))

    def test_ensure_false_reports_missing_without_creating(self):
        result = obsidian.daily_note(date="2026-03-10", ensure=False)
        self.assertFalse(result["existed"])
        self.assertIsNone(result["content"])
        self.assertFalse(os.path.exists(os.path.join(self.vault, "2026-03-10.md")))

    def test_a_second_call_finds_the_first_instead_of_recreating(self):
        obsidian.append_note(path="2026-03-10.md", content="already here")
        result = obsidian.daily_note(date="2026-03-10")
        self.assertTrue(result["existed"])
        self.assertEqual(result["content"], "already here")

    def test_daily_folder_is_honored(self):
        obsidian.DAILY_FOLDER = "Journal"
        result = obsidian.daily_note(date="2026-03-10")
        self.assertEqual(result["path"], "Journal/2026-03-10.md")

    def test_a_malformed_date_is_named_not_a_stack_trace(self):
        with self.assertRaises(obsidian.ToolError):
            obsidian.daily_note(date="not-a-date")


class TestListNotes(VaultCase):
    def test_recursive_by_default(self):
        self.write("A.md", "x")
        self.write("Sub/B.md", "x")
        result = obsidian.list_notes()
        self.assertEqual(result["total"], 2)

    def test_non_recursive_stays_in_the_named_folder(self):
        self.write("A.md", "x")
        self.write("Sub/B.md", "x")
        result = obsidian.list_notes(recursive=False)
        self.assertEqual(result["total"], 1)

    def test_title_comes_from_the_first_heading_when_present(self):
        self.write("A.md", "---\nstatus: x\n---\n# Real Title\nbody")
        result = obsidian.list_notes()
        self.assertEqual(result["notes"][0]["title"], "Real Title")

    def test_title_falls_back_to_filename_with_no_heading(self):
        self.write("Untitled Thoughts.md", "just body text, no heading")
        result = obsidian.list_notes()
        self.assertEqual(result["notes"][0]["title"], "Untitled Thoughts")

    def test_dotfolders_are_excluded_from_listing(self):
        self.write(".obsidian/plugin-notes.md", "not a real note")
        self.write("Real.md", "x")
        result = obsidian.list_notes()
        self.assertEqual(result["total"], 1)


if __name__ == "__main__":
    unittest.main()
