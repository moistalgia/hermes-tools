"""mcpkit: argument coercion and dispatch.

These are the two places every tool in the repo passes through, so a bug here
is a bug in all four servers at once. The coercion tests exist because two
callers send arguments and neither is careful - the CLI turns bare digits into
ints, agents send "40" for an integer field - and the whole point of coerce()
is that no tool has to defend itself against either.
"""

import unittest

from support import load_mcpkit

kit = load_mcpkit()


def define(schema, required=None):
    """A tool that just hands back what it was given.

    Built with explicit named parameters rather than **kwargs on purpose:
    call_tool filters arguments against the real signature, so a **kwargs tool
    would accept everything and quietly test nothing.
    """
    kit.TOOLS.clear()
    names = list(schema)
    source = (f"def echo({', '.join(f'{k}=None' for k in names)}):\n"
              f"    return {{'ok': True, 'got': {{{', '.join(f'{k!r}: {k}' for k in names)}}}}}\n")
    namespace = {}
    exec(source, namespace)  # noqa: S102 - a fixture, not user input
    return kit.tool("echo", schema, required=required)(namespace["echo"])


class TestCoercion(unittest.TestCase):
    def test_string_field_accepts_a_number(self):
        # The CLI parses qty=2 into an int before the tool ever sees it.
        define({"qty": kit.s("how much")})
        result = kit.call_tool("echo", {"qty": 2})
        self.assertEqual(result["got"]["qty"], "2")

    def test_integer_field_accepts_a_string(self):
        define({"pct": kit.i("percent")})
        result = kit.call_tool("echo", {"pct": " 40 "})
        self.assertEqual(result["got"]["pct"], 40)

    def test_integer_field_rejects_words_with_a_readable_error(self):
        define({"pct": kit.i("percent")})
        result = kit.call_tool("echo", {"pct": "quite bright"})
        self.assertFalse(result["ok"])
        self.assertIn("whole number", result["error"])
        self.assertIn("quite bright", result["error"])

    def test_number_field_keeps_the_fraction(self):
        # Celsius thermostats move in halves; int() here would silently make
        # 20.5 unreachable.
        define({"target": kit.n("degrees")})
        self.assertEqual(kit.call_tool("echo", {"target": "20.5"})["got"]["target"], 20.5)

    def test_number_field_rejects_words(self):
        define({"target": kit.n("degrees")})
        result = kit.call_tool("echo", {"target": "warm"})
        self.assertFalse(result["ok"])
        self.assertIn("must be a number", result["error"])

    def test_boolean_field_reads_the_usual_spellings(self):
        define({"flag": kit.b("a flag")})
        for text, expected in [("true", True), ("YES", True), ("1", True),
                               ("on", True), ("false", False), ("no", False),
                               ("", False)]:
            self.assertIs(kit.call_tool("echo", {"flag": text})["got"]["flag"], expected,
                          msg=f"for {text!r}")

    def test_none_survives_untouched(self):
        define({"qty": kit.s("how much")})
        self.assertIsNone(kit.call_tool("echo", {"qty": None})["got"]["qty"])


class TestDispatch(unittest.TestCase):
    def test_unknown_tool_lists_what_exists(self):
        define({})
        result = kit.call_tool("nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("echo", result["available_tools"])

    def test_unexpected_arguments_are_reported_not_swallowed(self):
        # A silently dropped argument looks like the tool ignoring an
        # instruction, which is the hardest kind of bug to see from a transcript.
        define({"qty": kit.s("how much")})
        result = kit.call_tool("echo", {"qty": "2", "colour": "blue"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["ignored_arguments"], ["colour"])

    def test_tool_errors_come_back_as_data_with_their_extras(self):
        kit.TOOLS.clear()

        @kit.tool("fail", {})
        def failing():
            raise kit.ToolError("No room named 'back room'.", known_rooms=["office"])

        result = kit.call_tool("failing", {})
        self.assertFalse(result["ok"])
        self.assertIn("back room", result["error"])
        self.assertEqual(result["known_rooms"], ["office"])

    def test_unexpected_exceptions_do_not_escape(self):
        # call_tool never raises: an exception reaching the JSON-RPC loop would
        # take the server down mid-conversation.
        kit.TOOLS.clear()

        @kit.tool("boom", {})
        def boom():
            raise RuntimeError("kaboom")

        result = kit.call_tool("boom", {})
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError: kaboom", result["error"])
        self.assertIn("verbatim", result["hint"])

    def test_a_non_dict_return_is_wrapped(self):
        kit.TOOLS.clear()

        @kit.tool("plain", {})
        def plain():
            return "just a string"

        self.assertEqual(kit.call_tool("plain", {}), {"ok": True, "result": "just a string"})


class TestCliParsing(unittest.TestCase):
    def test_key_value_pairs_with_light_coercion(self):
        args = kit.parse_cli_args(["room=office", "pct=40", "verbose=true", "temp=-5"])
        self.assertEqual(args, {"room": "office", "pct": 40, "verbose": True, "temp": -5})

    def test_a_value_with_spaces_survives(self):
        self.assertEqual(kit.parse_cli_args(["title=clean the gutters"]),
                         {"title": "clean the gutters"})

    def test_a_bare_token_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(SystemExit):
            kit.parse_cli_args(["office"])


class TestSchemas(unittest.TestCase):
    def test_enum_and_bounds_reach_the_schema(self):
        define({"state": kit.s("on or off", enum=["on", "off"]),
                "pct": kit.i("percent", minimum=1, maximum=100)})
        schema = kit.TOOLS["echo"]["inputSchema"]
        self.assertEqual(schema["properties"]["state"]["enum"], ["on", "off"])
        self.assertEqual(schema["properties"]["pct"]["minimum"], 1)
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
