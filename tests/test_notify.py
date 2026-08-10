"""notify-mcp: the read cursor, and keeping the channel worth reading.

Two things here would be invisible until they had already done damage. A cursor
that does not advance replays old messages and files them a second time; a
backend that reads its own outbound posts back in files them as user input,
which looks exactly like a haunting.
"""

import json
import os
import tempfile
import unittest

from support import load

TMP = tempfile.mkdtemp(prefix="notify-mcp-test-")

notify = load("notify_under_test", "notify-mcp/notify_mcp_server.py", env={
    "NOTIFY_BACKEND": "telegram",
    "NOTIFY_STATE": os.path.join(TMP, "notify.json"),
    "TELEGRAM_TOKEN": "test-token",
    "TELEGRAM_CHAT_ID": "12345",
})


class FakeHttp:
    """Records what was sent and hands back canned bodies."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def __call__(self, url, data=None, headers=None, method=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return 200, self.responses.pop(0) if self.responses else "{}"


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        notify.http = self.http
        notify.STATE_FILE = os.path.join(TMP, f"{self.id().rsplit('.', 1)[-1]}.json")
        for backend, value in (("BACKEND", "telegram"),):
            setattr(notify, backend, value)

    def use(self, backend, **env):
        notify.BACKEND = backend
        for key, value in env.items():
            setattr(notify, key, value)


class TestSending(NotifyCase):
    def test_telegram_carries_the_title_and_the_body(self):
        result = notify.notify(message="the back door has been open 40 minutes",
                               title="House")
        self.assertTrue(result["ok"])
        sent = self.http.calls[0]["data"]
        self.assertEqual(sent["chat_id"], "12345")
        self.assertIn("the back door", sent["text"])
        self.assertIn("*House*", sent["text"])

    def test_low_priority_arrives_without_a_buzz(self):
        notify.notify(message="fyi", priority="low")
        self.assertEqual(self.http.calls[0]["data"]["disable_notification"], "true")

    def test_normal_priority_does_buzz(self):
        notify.notify(message="something happened")
        self.assertEqual(self.http.calls[0]["data"]["disable_notification"], "false")

    def test_an_unknown_priority_lists_the_four_words(self):
        with self.assertRaises(notify.ToolError) as caught:
            notify.notify(message="hello", priority="screaming")
        self.assertEqual(caught.exception.extra["known_priorities"],
                         ["low", "normal", "high", "urgent"])

    def test_an_empty_message_is_refused(self):
        with self.assertRaises(notify.ToolError):
            notify.notify(message="   ")

    def test_missing_configuration_names_the_variable(self):
        # "not configured" sends someone reading logs to the wrong file.
        self.use("telegram", TELEGRAM_CHAT_ID="")
        try:
            with self.assertRaises(notify.ToolError) as caught:
                notify.notify(message="hello")
            self.assertEqual(caught.exception.extra["missing"], ["TELEGRAM_CHAT_ID"])
        finally:
            self.use("telegram", TELEGRAM_CHAT_ID="12345")

    def test_each_backend_gets_its_own_priority_numbering(self):
        # None of the three agree, which is why the agent only learns four words.
        self.use("ntfy", NTFY_TOPIC="house")
        try:
            notify.notify(message="water on the floor", priority="urgent")
            self.assertEqual(self.http.calls[0]["headers"]["Priority"], "5")
        finally:
            self.use("telegram")


class TestInbox(NotifyCase):
    def telegram_updates(self, *messages):
        return json.dumps({"ok": True, "result": [
            {"update_id": 100 + n,
             "message": {"message_id": n, "text": text, "date": 1770000000,
                         "from": {"first_name": "Sam"}, "chat": {"id": 12345}}}
            for n, text in enumerate(messages)]})

    def test_messages_come_back_unfiled(self):
        # inbox_fetch returns messages and files nothing. The skill decides what
        # a message *is*; otherwise this becomes a second, worse state store.
        self.http.responses.append(self.telegram_updates("add olive oil to the list"))
        result = notify.inbox_fetch()
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["from"], "Sam")
        self.assertEqual(result["messages"][0]["text"], "add olive oil to the list")

    def test_the_cursor_advances_so_nothing_is_filed_twice(self):
        self.http.responses.append(self.telegram_updates("first", "second"))
        notify.inbox_fetch()
        with open(notify.STATE_FILE, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["telegram_offset"], 102)
        # A second fetch asks Telegram to start from there.
        self.http.responses.append(json.dumps({"ok": True, "result": []}))
        result = notify.inbox_fetch()
        self.assertEqual(self.http.calls[1]["data"]["offset"], "102")
        self.assertEqual(result["messages"], [])

    def test_mark_read_false_leaves_the_cursor_alone(self):
        self.http.responses.append(self.telegram_updates("something"))
        notify.inbox_fetch(mark_read=False)
        self.assertFalse(os.path.exists(notify.STATE_FILE))

    def test_ntfy_does_not_read_its_own_notifications_back(self):
        self.use("ntfy", NTFY_TOPIC="house")
        try:
            self.http.responses.append("\n".join([
                json.dumps({"event": "message", "id": "a", "time": 1770000000,
                            "title": "Hermes", "message": "the brief"}),
                json.dumps({"event": "message", "id": "b", "time": 1770000001,
                            "message": "buy milk"}),
            ]))
            result = notify.inbox_fetch()
            self.assertEqual([m["text"] for m in result["messages"]], ["buy milk"])
        finally:
            self.use("telegram")

    def test_an_outbound_only_backend_says_so_and_names_the_alternatives(self):
        self.use("pushover")
        try:
            with self.assertRaises(notify.ToolError) as caught:
                notify.inbox_fetch()
            self.assertIn("can only send", str(caught.exception))
            self.assertEqual(caught.exception.extra["inbound_backends"], ["telegram", "ntfy"])
        finally:
            self.use("telegram")


class TestStatus(NotifyCase):
    def test_status_reports_what_is_missing_rather_than_just_failing(self):
        self.use("telegram", TELEGRAM_TOKEN="")
        try:
            result = notify.notify_status()
            self.assertFalse(result["ok"])
            self.assertIn("TELEGRAM_TOKEN", result["missing"])
        finally:
            self.use("telegram", TELEGRAM_TOKEN="test-token")

    def test_status_says_whether_inbound_is_possible(self):
        self.assertTrue(notify.notify_status()["inbound_supported"])
        self.use("pushover", PUSHOVER_TOKEN="t", PUSHOVER_USER="u")
        try:
            self.assertFalse(notify.notify_status()["inbound_supported"])
        finally:
            self.use("telegram")

    def test_an_unknown_backend_lists_the_real_ones(self):
        self.use("carrier-pigeon")
        try:
            with self.assertRaises(notify.ToolError) as caught:
                notify.notify_status()
            self.assertEqual(caught.exception.extra["known_backends"],
                             ["ntfy", "pushover", "telegram"])
        finally:
            self.use("telegram")


if __name__ == "__main__":
    unittest.main()
