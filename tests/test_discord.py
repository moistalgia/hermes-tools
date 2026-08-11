"""discord-mcp: the closed recipient list, and the two-call DM sequence.

Two things here would be invisible until they had already done damage. A
recipient resolved from anything other than `DISCORD_DM_USERS` means the
agent could DM an arbitrary snowflake it was handed by untrusted text; a DM
that opens the channel but never posts to it looks identical to one that
worked, from the caller's side, unless both calls are checked.
"""

import unittest

from support import load

discord = load("discord_under_test", "discord-mcp/discord_mcp_server.py", env={
    "DISCORD_BOT_TOKEN": "test-token",
    "DISCORD_DM_USERS": "nathan=111111111111111111,anna=222222222222222222",
})


class FakeHttp:
    """Records what was sent and hands back canned bodies, in order."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def __call__(self, url, payload=None, method=None):
        self.calls.append({"url": url, "payload": payload, "method": method})
        if self.responses:
            return self.responses.pop(0)
        return 200, {}


class DiscordCase(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        discord.http = self.http
        discord.BOT_TOKEN = "test-token"
        discord.DM_USERS = {"nathan": "111111111111111111", "anna": "222222222222222222"}


class TestSending(DiscordCase):
    def test_dm_opens_the_channel_then_posts_to_it(self):
        self.http.responses.append((200, {"id": "channel-9"}))
        result = discord.discord_dm(user="nathan", message="the brief")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.http.calls), 2)

        opened = self.http.calls[0]
        self.assertEqual(opened["url"], f"{discord.API}/users/@me/channels")
        self.assertEqual(opened["payload"], {"recipient_id": "111111111111111111"})

        posted = self.http.calls[1]
        self.assertEqual(posted["url"], f"{discord.API}/channels/channel-9/messages")
        self.assertEqual(posted["payload"], {"content": "the brief"})

    def test_the_recipient_name_is_case_insensitive(self):
        self.http.responses.append((200, {"id": "channel-1"}))
        discord.discord_dm(user="Nathan", message="hi")
        self.assertEqual(self.http.calls[0]["payload"]["recipient_id"], "111111111111111111")

    def test_an_unknown_name_lists_the_real_ones_not_the_id_it_guessed(self):
        with self.assertRaises(discord.ToolError) as caught:
            discord.discord_dm(user="the discord server admin", message="hi")
        self.assertEqual(caught.exception.extra["known_users"], ["anna", "nathan"])
        self.assertEqual(self.http.calls, [])  # never touches the network

    def test_an_empty_message_is_refused(self):
        with self.assertRaises(discord.ToolError):
            discord.discord_dm(user="nathan", message="   ")
        self.assertEqual(self.http.calls, [])

    def test_a_message_over_the_discord_limit_is_refused_not_truncated(self):
        with self.assertRaises(discord.ToolError) as caught:
            discord.discord_dm(user="nathan", message="x" * 2001)
        self.assertEqual(caught.exception.extra["limit"], 2000)
        self.assertEqual(self.http.calls, [])

    def test_missing_bot_token_names_the_variable(self):
        discord.BOT_TOKEN = ""
        try:
            with self.assertRaises(discord.ToolError) as caught:
                discord.discord_dm(user="nathan", message="hi")
            self.assertEqual(caught.exception.extra["missing"], ["DISCORD_BOT_TOKEN"])
        finally:
            discord.BOT_TOKEN = "test-token"

    def test_a_channel_open_that_hands_back_no_id_is_reported_not_swallowed(self):
        self.http.responses.append((200, {"weird": "response"}))
        with self.assertRaises(discord.ToolError) as caught:
            discord.discord_dm(user="nathan", message="hi")
        self.assertIn("did not hand back a DM channel id", str(caught.exception))
        # Never posts a message to a channel it doesn't have.
        self.assertEqual(len(self.http.calls), 1)


class TestStatus(DiscordCase):
    def test_status_reports_who_is_reachable(self):
        result = discord.discord_status()
        self.assertTrue(result["ok"])
        self.assertEqual(result["known_users"], ["anna", "nathan"])

    def test_status_names_a_missing_token_rather_than_saying_not_configured(self):
        discord.BOT_TOKEN = ""
        try:
            result = discord.discord_status()
            self.assertFalse(result["ok"])
            self.assertIn("DISCORD_BOT_TOKEN", result["missing"])
        finally:
            discord.BOT_TOKEN = "test-token"

    def test_status_names_an_empty_recipient_list(self):
        discord.DM_USERS = {}
        try:
            result = discord.discord_status()
            self.assertFalse(result["ok"])
            self.assertIn("DISCORD_DM_USERS", result["missing"])
        finally:
            discord.DM_USERS = {"nathan": "111111111111111111", "anna": "222222222222222222"}


class TestParsing(unittest.TestCase):
    def test_a_malformed_entry_is_dropped_not_fatal(self):
        # The server should still come up and say what parsed, rather than
        # failing the whole MCP handshake over one typo in the list.
        users = discord._parse_users("nathan=111111111111111111, not-a-pair, anna=abc, =222")
        self.assertEqual(users, {"nathan": "111111111111111111"})

    def test_names_are_lowercased_on_read(self):
        users = discord._parse_users("Nathan=111111111111111111")
        self.assertEqual(users, {"nathan": "111111111111111111"})


if __name__ == "__main__":
    unittest.main()
