#!/usr/bin/env python3
"""
notify-mcp - the channel the agent talks to you through when you are not here.

An assistant that only answers is a tool. One that can start the conversation -
"the back door has been open 40 minutes and it is 31F outside" - is something
else. That needs two halves: something that fires runs on a schedule, and
somewhere to put the output that you actually read. This is the second half.

Email is where reminders go to die. The backends here are the ones people
actually look at:

    telegram  default. Real conversations, replies, and more than one person in
              the household can message the same bot.
    ntfy      no account, self-hostable. The phone app publishes back to the
              same topic, so inbound works, but it is a text field rather than
              a conversation.
    pushover  outbound only, but the most reliable delivery of the three.

Getting TELEGRAM_CHAT_ID: create a bot with @BotFather, set TELEGRAM_TOKEN,
message the bot once from your phone, then run inbox_fetch. The chat_id comes
back with the message. inbox_fetch deliberately does not require the chat id,
so that this bootstrap works.

Inbound is why this server has more than one tool. `inbox_fetch` pulls messages
you sent from your phone. It deliberately does **not** write them anywhere -
the skill decides whether a message is a task, a shopping item, or noise, and
files it through state-mcp. Keeping transport and meaning in separate servers
is what stops this from becoming a second, worse state store.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python notify_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python notify_mcp_server.py notify_status
         python notify_mcp_server.py notify message="test" title="Hermes"

Environment: see notify_status, which prints exactly what is set and what is
missing for the selected backend.

No dependencies. urllib and the standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

BACKEND = os.environ.get("NOTIFY_BACKEND", "telegram").strip().lower()
TIMEOUT = int(os.environ.get("NOTIFY_TIMEOUT", "15"))
# ~/.hermes, beside Hermes' own config. Lose this file and the next fetch
# replays old messages, which get filed a second time.
STATE_FILE = os.environ.get("NOTIFY_STATE") or os.path.join(
    os.path.expanduser("~"), ".hermes", "notify.json")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

PRIORITIES = ["low", "normal", "high", "urgent"]

# Each backend numbers priority differently and none of them agree. Normalise
# once here so the agent only ever learns four words.
NTFY_PRIORITY = {"low": "2", "normal": "3", "high": "4", "urgent": "5"}
PUSHOVER_PRIORITY = {"low": "-1", "normal": "0", "high": "1", "urgent": "1"}


# ---------------------------------------------------------------------------
# Cursor
#
# Inbound polling needs to remember what it has already seen, or every fetch
# replays the whole history and the agent files the same message five times.
# One small JSON file, next to the state database.
# ---------------------------------------------------------------------------


def load_cursor():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cursor(data):
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http(url, data=None, headers=None, method=None):
    body = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
    elif isinstance(data, str):
        body = data.encode("utf-8")
    elif isinstance(data, bytes):
        body = data
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ToolError(
            f"{BACKEND} returned HTTP {exc.code}: {detail or exc.reason}. "
            + backend_hint(exc.code)
        )
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Could not reach {urllib.parse.urlparse(url).netloc}: {exc.reason}. "
            "The container has no route to it, or the server name is wrong. "
            "Check from inside the container before changing anything here."
        )


def backend_hint(code):
    if code in (401, 403):
        return {
            "ntfy": "The topic is protected and NTFY_TOKEN is missing or wrong.",
            "telegram": "TELEGRAM_TOKEN is wrong or has been revoked.",
            "pushover": "PUSHOVER_TOKEN or PUSHOVER_USER is wrong.",
        }.get(BACKEND, "Credentials are wrong.")
    if code == 404:
        return "The URL is wrong for this backend - check the server address."
    if code == 400 and BACKEND == "telegram":
        return ("Usually TELEGRAM_CHAT_ID is wrong. Message the bot from your "
                "phone once, then run inbox_fetch to discover the real chat id.")
    return ""


def require(*pairs):
    """Fail with the exact variable names that are missing, not 'not configured'."""
    missing = [name for name, value in pairs if not value]
    if missing:
        raise ToolError(
            f"Backend {BACKEND!r} needs {', '.join(missing)} set in the MCP config. "
            f"Run notify_status to see everything this backend requires.",
            backend=BACKEND, missing=missing,
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool("Check the notification channel: which backend, what is configured, what is missing. Run this first when a message does not arrive.")
def notify_status():
    known = {
        "ntfy": [("NTFY_TOPIC", NTFY_TOPIC)],
        "telegram": [("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)],
        "pushover": [("PUSHOVER_TOKEN", PUSHOVER_TOKEN), ("PUSHOVER_USER", PUSHOVER_USER)],
    }
    if BACKEND not in known:
        raise ToolError(f"NOTIFY_BACKEND={BACKEND!r} is not a backend this server has.",
                        known_backends=sorted(known))
    required = known[BACKEND]
    missing = [name for name, value in required if not value]
    cursor = load_cursor()
    inbound = BACKEND in ("ntfy", "telegram")
    summary = (f"Backend {BACKEND}. "
               + ("Configured." if not missing else f"MISSING: {', '.join(missing)}.")
               + (" Inbound supported." if inbound else " Outbound only - no inbound on this backend."))
    if BACKEND == "ntfy":
        summary += f" Topic {NTFY_TOPIC or '(unset)'} on {NTFY_SERVER}."
    return {
        "ok": not missing, "summary": summary, "backend": BACKEND,
        "missing": missing, "inbound_supported": inbound,
        "state_file": STATE_FILE, "cursor": cursor,
        "error": None if not missing else f"Missing: {', '.join(missing)}",
    }


@tool(
    "Send a push notification. This reaches the user's phone, so it is for "
    "things worth interrupting them over - not acknowledgements and not "
    "anything they will see next time they open a conversation.",
    {"message": s("The body. Write it so it is useful on a lock screen: what happened and what to do."),
     "title": s("Short heading. Defaults to 'Hermes'."),
     "priority": s("How loudly. 'urgent' bypasses quiet hours on most phones - use it for water, fire, and doors.",
                   default="normal", enum=PRIORITIES),
     "tags": s("Comma-separated. ntfy renders known names as emoji: 'warning', 'house', 'calendar'."),
     "link": s("A URL to attach, if there is somewhere useful to go.")},
    required=["message"],
)
def notify(message, title=None, priority="normal", tags=None, link=None):
    message = (message or "").strip()
    if not message:
        raise ToolError("Nothing to send.")
    priority = (priority or "normal").lower()
    if priority not in PRIORITIES:
        raise ToolError(f"Unknown priority {priority!r}.", known_priorities=PRIORITIES)
    title = title or "Hermes"

    if BACKEND == "ntfy":
        require(("NTFY_TOPIC", NTFY_TOPIC))
        headers = {
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": NTFY_PRIORITY[priority],
            "Content-Type": "text/plain; charset=utf-8",
        }
        if tags:
            headers["Tags"] = tags
        if link:
            headers["Click"] = link
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        http(f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}", data=message, headers=headers)

    elif BACKEND == "telegram":
        require(("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID))
        text = f"*{title}*\n{message}" + (f"\n{link}" if link else "")
        http(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
             data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown",
                   "disable_notification": "true" if priority == "low" else "false"})

    elif BACKEND == "pushover":
        require(("PUSHOVER_TOKEN", PUSHOVER_TOKEN), ("PUSHOVER_USER", PUSHOVER_USER))
        payload = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "message": message,
                   "title": title, "priority": PUSHOVER_PRIORITY[priority]}
        if link:
            payload["url"] = link
        http("https://api.pushover.net/1/messages.json", data=payload)

    else:
        raise ToolError(f"NOTIFY_BACKEND={BACKEND!r} is not a backend this server has.",
                        known_backends=["ntfy", "telegram", "pushover"])

    return {"ok": True,
            "summary": f"Sent via {BACKEND} ({priority}): {title} — {message[:120]}",
            "backend": BACKEND, "priority": priority}


@tool(
    "Fetch messages the user sent in from their phone since the last fetch. "
    "This returns them and nothing else — decide what each one means and file "
    "it yourself through state-mcp (usually capture_add). Advancing the read "
    "cursor is what stops the same message being filed twice, so leave "
    "mark_read on unless you are debugging.",
    {"mark_read": b("Advance the cursor past what is returned.", default=True),
     "limit": i("Most messages to return.", default=25)},
)
def inbox_fetch(mark_read=True, limit=25):
    cursor = load_cursor()
    messages = []

    if BACKEND == "telegram":
        require(("TELEGRAM_TOKEN", TELEGRAM_TOKEN))
        offset = cursor.get("telegram_offset", 0)
        _, body = http(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                       data={"offset": str(offset), "limit": str(limit), "timeout": "0"})
        payload = json.loads(body)
        if not payload.get("ok"):
            raise ToolError(f"Telegram refused getUpdates: {payload.get('description')}")
        highest = offset
        for update in payload.get("result", []):
            highest = max(highest, update.get("update_id", 0) + 1)
            msg = update.get("message") or update.get("edited_message")
            if not msg or not msg.get("text"):
                continue
            sender = msg.get("from") or {}
            messages.append({
                "id": str(msg.get("message_id")),
                "text": msg["text"],
                "from": sender.get("first_name") or sender.get("username") or "unknown",
                "chat_id": (msg.get("chat") or {}).get("id"),
                "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.get("date", 0))),
            })
        if mark_read:
            cursor["telegram_offset"] = highest
            save_cursor(cursor)

    elif BACKEND == "ntfy":
        require(("NTFY_TOPIC", NTFY_TOPIC))
        since = cursor.get("ntfy_since") or int(time.time()) - 86400
        headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
        _, body = http(f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}/json"
                       f"?poll=1&since={since}", headers=headers)
        newest = since
        for line in body.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "message":
                continue
            newest = max(newest, event.get("time", since))
            # Our own outbound posts land on the same topic. Without this the
            # agent reads its own notifications back and files them as user
            # input, which looks exactly like a haunting.
            if (event.get("title") or "").strip() == "Hermes":
                continue
            messages.append({
                "id": event.get("id"),
                "text": event.get("message", ""),
                "from": "phone",
                "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(event.get("time", 0))),
            })
        if mark_read:
            cursor["ntfy_since"] = newest + 1
            save_cursor(cursor)

    else:
        raise ToolError(
            f"Backend {BACKEND!r} has no inbound path — it can only send. "
            "Switch NOTIFY_BACKEND to 'telegram' or 'ntfy' if you want to be "
            "able to message the house from your phone.",
            inbound_backends=["telegram", "ntfy"],
        )

    messages = messages[:limit]
    if not messages:
        return {"ok": True, "summary": "No new inbound messages.", "messages": []}
    return {
        "ok": True,
        "summary": f"{len(messages)} new: " + " | ".join(
            f"{m['from']}: {m['text'][:90]}" for m in messages),
        "messages": messages,
        "cursor_advanced": mark_read,
    }


def banner():
    lines = [f"NOTIFY_BACKEND={BACKEND}", f"NOTIFY_STATE={STATE_FILE}"]
    if BACKEND == "ntfy":
        lines.append(f"NTFY_SERVER={NTFY_SERVER}  NTFY_TOPIC={NTFY_TOPIC or 'MISSING'}")
    elif BACKEND == "telegram":
        lines.append(f"TELEGRAM_TOKEN={'set' if TELEGRAM_TOKEN else 'MISSING'}  "
                     f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID or 'MISSING'}")
    elif BACKEND == "pushover":
        lines.append(f"PUSHOVER_TOKEN={'set' if PUSHOVER_TOKEN else 'MISSING'}  "
                     f"PUSHOVER_USER={'set' if PUSHOVER_USER else 'MISSING'}")
    return "  ".join(lines)


if __name__ == "__main__":
    run("notify-mcp", "1.0", banner)
