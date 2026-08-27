"""The stdio handshake, run as a real subprocess.

Everything else in this suite imports the servers and calls functions. This
file does not: it launches each one the way Hermes does and talks JSON-RPC to
it, because the failures that actually happen in deployment live in the gap
between "the function works" and "the process speaks the protocol".

The strict rule being enforced is that **nothing but JSON-RPC frames may ever
reach stdout**. One stray print - here, in a dependency, in a warning - corrupts
the stream and the handshake fails with no useful error at all.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="serve-test-")

# A scratch database, never the real one. These subprocesses write.
SERVERS = {
    "state": ("state-mcp/state_mcp_server.py", {"STATE_DB": os.path.join(TMP, "household.db")}),
    "notify": ("notify-mcp/notify_mcp_server.py", {"NOTIFY_STATE": os.path.join(TMP, "notify.json")}),
    "hass": ("hass-mcp/hass_mcp_server.py", {"HASS_MAP": os.path.join(TMP, "house.json")}),
    # No credentials on purpose, same reasoning as prowlarr below - the
    # handshake and tool list must come up before anything is configured.
    "discord": ("discord-mcp/discord_mcp_server.py", {"DISCORD_BOT_TOKEN": "", "DISCORD_DM_USERS": ""}),
    # No credentials on purpose. The handshake and the tool list must come up
    # without them - a server that needs a working backend before it can
    # advertise its tools fails at startup with nothing to read.
    "prowlarr": ("prowlarr-mcp/prowlarr_mcp_server.py", {"PROWLARR_API_KEY": ""}),
    "obsidian": ("obsidian-mcp/obsidian_mcp_server.py", {"OBSIDIAN_VAULT_PATH": os.path.join(TMP, "vault")}),
}

HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


def talk(relative_path, requests, env_extra=None):
    """Run a server, send requests, return the parsed replies."""
    env = dict(os.environ)
    env.update(env_extra or {})
    # Unbuffered, so a crash cannot swallow output that was already written.
    process = subprocess.run(
        [sys.executable, "-u", os.path.join(ROOT, relative_path), "serve"],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True, text=True, env=env, timeout=60)
    return process


class TestHandshake(unittest.TestCase):
    def check(self, name, relative_path, env_extra):
        process = talk(relative_path, HANDSHAKE, env_extra)
        lines = [line for line in process.stdout.splitlines() if line.strip()]

        # Every line must parse. A single unparseable one is the whole failure
        # mode this test exists for, so report what it actually said.
        replies = []
        for line in lines:
            try:
                replies.append(json.loads(line))
            except json.JSONDecodeError:
                self.fail(f"{name} wrote non-JSON to stdout: {line!r}\n"
                          f"stderr was:\n{process.stderr[-2000:]}")
        return replies

    def test_every_server_completes_the_handshake(self):
        for name, (path, env_extra) in SERVERS.items():
            with self.subTest(server=name):
                replies = self.check(name, path, env_extra)
                self.assertEqual(len(replies), 2,
                                 msg="a notification must not get a reply")
                initialize, tools = replies
                self.assertEqual(initialize["id"], 1)
                self.assertEqual(initialize["result"]["serverInfo"]["name"], f"{name}-mcp")
                self.assertIn("protocolVersion", initialize["result"])
                self.assertEqual(tools["id"], 2)
                self.assertTrue(tools["result"]["tools"])

    def test_every_tool_advertises_a_usable_schema(self):
        for name, (path, env_extra) in SERVERS.items():
            with self.subTest(server=name):
                _initialize, tools = self.check(name, path, env_extra)
                for entry in tools["result"]["tools"]:
                    self.assertTrue(entry["description"].strip(),
                                    msg=f"{name}.{entry['name']} has no description")
                    schema = entry["inputSchema"]
                    self.assertEqual(schema["type"], "object")
                    # Every required argument must actually be declared, or the
                    # agent is told to send something with no type or meaning.
                    for required in schema["required"]:
                        self.assertIn(required, schema["properties"],
                                      msg=f"{name}.{entry['name']} requires an undeclared field")

    def test_an_unknown_method_gets_an_error_not_a_crash(self):
        process = talk(SERVERS["state"][0],
                       [{"jsonrpc": "2.0", "id": 1, "method": "resources/list"}],
                       SERVERS["state"][1])
        reply = json.loads(process.stdout.strip())
        self.assertEqual(reply["error"]["code"], -32601)

    def test_garbage_on_stdin_does_not_take_the_server_down(self):
        # A partial write from a client should cost one line, not the session.
        process = subprocess.run(
            [sys.executable, "-u", os.path.join(ROOT, SERVERS["state"][0]), "serve"],
            input="{not json at all\n"
                  + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n",
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, **SERVERS["state"][1]))
        replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(replies[-1]["id"], 7)


class TestUsageGoesToStderr(unittest.TestCase):
    def test_running_without_arguments_keeps_stdout_clean(self):
        # A typo in config.yaml that drops the `serve` argument would otherwise
        # put usage text on the protocol channel and fail the handshake with no
        # explanation at all.
        for name, (path, env_extra) in SERVERS.items():
            with self.subTest(server=name):
                process = subprocess.run(
                    [sys.executable, os.path.join(ROOT, path)],
                    capture_output=True, text=True, timeout=60,
                    env=dict(os.environ, **env_extra))
                self.assertEqual(process.stdout.strip(), "")
                self.assertIn("MCP server / CLI", process.stderr)


class TestCliMatchesMcp(unittest.TestCase):
    def test_the_cli_runs_the_same_tools(self):
        # The convention the whole repo rests on: prove a call from a shell,
        # the agent makes the identical call over MCP.
        process = subprocess.run(
            [sys.executable, os.path.join(ROOT, "state-mcp/state_mcp_server.py"),
             "task_add", "title=prove the cli works", "area=admin"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, STATE_DB=os.path.join(TMP, "cli-smoke.db")))
        payload = json.loads(process.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(process.returncode, 0)
        self.assertIn("prove the cli works", payload["summary"])

    def test_a_failing_cli_call_exits_nonzero_with_the_real_error(self):
        process = subprocess.run(
            [sys.executable, os.path.join(ROOT, "state-mcp/state_mcp_server.py"),
             "task_add", "title=nope", "area=the shed"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, STATE_DB=os.path.join(TMP, "cli-smoke.db")))
        payload = json.loads(process.stdout)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("known_areas", payload)


if __name__ == "__main__":
    unittest.main()
