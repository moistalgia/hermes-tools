"""Opt-in capture facility in mcpkit.

Each test gets its own isolated mcpkit instance (via support.load_mcpkit) and
its own temporary directory, so captures do not bleed across tests and the
working tree is never written to.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402


def fresh_kit(env=None):
    """A private mcpkit instance with the given environment already applied."""
    for key, value in (env or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    return support.load_mcpkit()


def register_ok(kit):
    kit.TOOLS.clear()

    @kit.tool("echo back", {"msg": kit.s("text")}, required=["msg"])
    def echo(msg):
        return {"ok": True, "echo": msg}

    return "echo"


def register_failing(kit):
    kit.TOOLS.clear()

    @kit.tool("always fails", {})
    def boom():
        raise kit.ToolError("deliberate failure")

    return "boom"


def register_exploding(kit):
    kit.TOOLS.clear()

    @kit.tool("raises unexpectedly", {})
    def explode():
        raise RuntimeError("unexpected kaboom")

    return "explode"


class TestCaptureOff(unittest.TestCase):
    """When HERMES_CAPTURE is unset, no file is created and no side-effects occur."""

    def setUp(self):
        os.environ.pop("HERMES_CAPTURE", None)
        os.environ.pop("HERMES_CAPTURE_DIR", None)

    def test_no_file_created_when_capture_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            kit = fresh_kit({"HERMES_CAPTURE": None,
                             "HERMES_CAPTURE_DIR": tmp})
            register_ok(kit)
            kit.call_tool("echo", {"msg": "hello"})
            self.assertEqual(os.listdir(tmp), [],
                             "no file should be written when HERMES_CAPTURE is unset")

    def test_empty_string_also_treated_as_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            kit = fresh_kit({"HERMES_CAPTURE": "",
                             "HERMES_CAPTURE_DIR": tmp})
            register_ok(kit)
            kit.call_tool("echo", {"msg": "hi"})
            self.assertEqual(os.listdir(tmp), [])

    def test_call_still_returns_correctly_when_capture_off(self):
        kit = fresh_kit({"HERMES_CAPTURE": None})
        register_ok(kit)
        result = kit.call_tool("echo", {"msg": "world"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["echo"], "world")


class TestCaptureOn(unittest.TestCase):
    """When HERMES_CAPTURE=1, a JSONL file is appended with a well-formed record."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["HERMES_CAPTURE"] = "1"
        os.environ["HERMES_CAPTURE_DIR"] = self._tmp

    def tearDown(self):
        os.environ.pop("HERMES_CAPTURE", None)
        os.environ.pop("HERMES_CAPTURE_DIR", None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _lines(self, server):
        path = os.path.join(self._tmp, f"{server}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_successful_call_appends_valid_record(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        result = kit.call_tool("echo", {"msg": "hello"})
        self.assertTrue(result["ok"])

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertIn("timestamp", rec)
        self.assertEqual(rec["server"], "testserver")
        self.assertEqual(rec["tool"], "echo")
        self.assertEqual(rec["args"], {"msg": "hello"})
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["result"]["echo"], "hello")
        # reasoning should be absent when not set
        self.assertNotIn("reasoning", rec)

    def test_tool_error_captured_with_ok_false(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_failing(kit)
        result = kit.call_tool("boom", {})
        self.assertFalse(result["ok"])

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertFalse(rec["ok"])
        self.assertIn("deliberate failure", rec["result"]["error"])

    def test_unexpected_exception_captured_with_ok_false(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_exploding(kit)
        result = kit.call_tool("explode", {})
        self.assertFalse(result["ok"])

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["ok"])

    def test_multiple_calls_append_separate_lines(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        kit.call_tool("echo", {"msg": "first"})
        kit.call_tool("echo", {"msg": "second"})

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["args"]["msg"], "first")
        self.assertEqual(lines[1]["args"]["msg"], "second")

    def test_unknown_tool_still_captured(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        kit.TOOLS.clear()
        result = kit.call_tool("nosuch", {})
        self.assertFalse(result["ok"])
        lines = self._lines("testserver")
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["ok"])

    def test_server_name_determines_filename(self):
        kit = fresh_kit()
        kit._server_name = "myserver"
        register_ok(kit)
        kit.call_tool("echo", {"msg": "x"})
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "myserver.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "mcpkit.jsonl")))


class TestCaptureReasoning(unittest.TestCase):
    """capture_reasoning() attaches text to the next call only and is cleared after."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["HERMES_CAPTURE"] = "1"
        os.environ["HERMES_CAPTURE_DIR"] = self._tmp

    def tearDown(self):
        os.environ.pop("HERMES_CAPTURE", None)
        os.environ.pop("HERMES_CAPTURE_DIR", None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _lines(self, server):
        path = os.path.join(self._tmp, f"{server}.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_reasoning_appears_in_record_when_set(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        kit.capture_reasoning("I chose echo because the user greeted me")
        kit.call_tool("echo", {"msg": "hi"})

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["reasoning"], "I chose echo because the user greeted me")

    def test_reasoning_cleared_after_first_call(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        kit.capture_reasoning("only for the first call")
        kit.call_tool("echo", {"msg": "first"})
        kit.call_tool("echo", {"msg": "second"})

        lines = self._lines("testserver")
        self.assertEqual(len(lines), 2)
        self.assertIn("reasoning", lines[0])
        self.assertNotIn("reasoning", lines[1])

    def test_reasoning_absent_when_never_set(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        kit.call_tool("echo", {"msg": "plain"})

        lines = self._lines("testserver")
        self.assertNotIn("reasoning", lines[0])


class TestCaptureWriteFailure(unittest.TestCase):
    """A write failure must not raise or change the tool's return value."""

    def setUp(self):
        # Point capture at a path that cannot be created / written to.
        os.environ["HERMES_CAPTURE"] = "1"
        os.environ["HERMES_CAPTURE_DIR"] = "/no/such/path/hermes_test_unwritable"

    def tearDown(self):
        os.environ.pop("HERMES_CAPTURE", None)
        os.environ.pop("HERMES_CAPTURE_DIR", None)

    def test_tool_result_unaffected_by_write_failure(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_ok(kit)
        # Should not raise; must return the normal success payload.
        result = kit.call_tool("echo", {"msg": "resilience"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["echo"], "resilience")

    def test_failed_tool_still_returns_ok_false_despite_write_failure(self):
        kit = fresh_kit()
        kit._server_name = "testserver"
        register_failing(kit)
        result = kit.call_tool("boom", {})
        self.assertFalse(result["ok"])


class TestCaptureCaptureDir(unittest.TestCase):
    """HERMES_CAPTURE_DIR overrides where the JSONL files land."""

    def test_custom_dir_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            subdir = os.path.join(tmp, "custom", "nested")
            kit = fresh_kit({"HERMES_CAPTURE": "1",
                             "HERMES_CAPTURE_DIR": subdir})
            kit._server_name = "srv"
            register_ok(kit)
            kit.call_tool("echo", {"msg": "check"})
            expected = os.path.join(subdir, "srv.jsonl")
            self.assertTrue(os.path.exists(expected))
            with open(expected, encoding="utf-8") as fh:
                rec = json.loads(fh.readline())
            self.assertEqual(rec["tool"], "echo")


if __name__ == "__main__":
    unittest.main()
