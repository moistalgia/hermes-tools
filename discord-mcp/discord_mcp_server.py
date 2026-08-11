#!/usr/bin/env python3
"""
discord-mcp - lets Hermes start a DM instead of only replying on whatever
surface it was invoked from.

Gladys (Hermes' Discord gateway) replies on the channel a conversation
happened on, and a scheduled job that delivers to Discord posts its final
response to the configured home channel - a channel the whole household
shares. Neither path can put a message in front of one specific person
without the rest of the house seeing it too. This server is the patch: one
tool, DM a named person, nothing else.

**Bot-to-bot DMs are blocked by Discord itself (error 50007) - bot-to-*user*
DMs are not.** This only ever targets a person, and that has always been a
normal thing for a bot to do, no different from any moderation bot's DM.

**Recipients are a closed, named list - never a raw snowflake id.** Same rule
DESIGN.md states for rooms and Plex players: "the agent never sees a raw
identifier." `DISCORD_DM_USERS` maps names to ids once, here, and the agent
only ever says who, never which eighteen-digit number.

This reuses the same bot token Gladys already runs on - it is one more
recipient of an existing bot's messages, not a second bot to invite and
manage. No new Discord application, no new intents: opening a DM channel and
posting to it needs nothing beyond what a bot already has.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python discord_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python discord_mcp_server.py discord_status
         python discord_mcp_server.py discord_dm user=nathan message="test"

No dependencies. urllib and the standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, run, s, tool  # noqa: E402

API = "https://discord.com/api/v10"
TIMEOUT = int(os.environ.get("DISCORD_TIMEOUT", "15"))
MAX_LEN = 2000  # Discord's hard limit on a message's content field.

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")


def _parse_users(raw):
    """`name=id,name=id` -> {lowercased name: id}.

    A malformed entry is dropped rather than failing the whole server at
    import time - the server should still come up and `discord_status` should
    still be able to say which recipients parsed and which did not, instead of
    the MCP handshake failing with no explanation.
    """
    users = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, uid = pair.partition("=")
        name = name.strip().lower()
        uid = uid.strip()
        if name and uid.isdigit():
            users[name] = uid
    return users


DM_USERS = _parse_users(os.environ.get("DISCORD_DM_USERS", ""))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http(url, payload=None, method=None):
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        detail = parsed.get("message") or raw[:200] or exc.reason
        raise ToolError(f"Discord returned HTTP {exc.code}: {detail}. " + hint(exc.code, parsed))
    except urllib.error.URLError as exc:
        raise ToolError(
            f"Could not reach discord.com: {exc.reason}. This host has no "
            "route to it, or Discord is down. Report this and stop - no "
            "argument variation changes that."
        )


def hint(code, parsed):
    inner = parsed.get("code")
    if code == 401:
        return "DISCORD_BOT_TOKEN is wrong or has been revoked."
    if code == 403 and inner == 50007:
        return ("This person has DMs from server members turned off, has no "
                "server in common with the bot, or has blocked it.")
    if code == 429:
        return "Rate limited - wait before retrying, do not immediately resend."
    return ""


def require(*pairs):
    """Fail with the exact variable names that are missing, not 'not configured'."""
    missing = [name for name, value in pairs if not value]
    if missing:
        raise ToolError(
            f"discord-mcp needs {', '.join(missing)} set in the MCP config. "
            "Run discord_status to see what is missing.",
            missing=missing,
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool("Check the Discord DM channel: is the bot token set, and who can be messaged. Run this first when discord_dm fails or before using an unfamiliar name.")
def discord_status():
    missing = [name for name, value in (("DISCORD_BOT_TOKEN", BOT_TOKEN), ("DISCORD_DM_USERS", DM_USERS)) if not value]
    summary = (
        ("Configured. " if not missing else f"MISSING: {', '.join(missing)}. ")
        + (f"Can message: {', '.join(sorted(DM_USERS))}." if DM_USERS else "No recipients configured.")
    )
    return {
        "ok": not missing, "summary": summary, "missing": missing,
        "known_users": sorted(DM_USERS),
        "error": None if not missing else f"Missing: {', '.join(missing)}",
    }


@tool(
    "Send a direct message to one named person - not to any Discord channel, "
    "and never to whichever channel a conversation happened to start on. Use "
    "this for anything meant for one person specifically: a scheduled brief, "
    "an alert, anything that should not become the whole household's "
    "business in a shared channel.",
    {"user": s("Who to message - a name from DISCORD_DM_USERS. Not a Discord id; run discord_status to see known names."),
     "message": s(f"The body. Plain text, up to {MAX_LEN} characters.")},
    required=["user", "message"],
)
def discord_dm(user, message):
    require(("DISCORD_BOT_TOKEN", BOT_TOKEN), ("DISCORD_DM_USERS", DM_USERS))
    key = (user or "").strip().lower()
    user_id = DM_USERS.get(key)
    if not user_id:
        raise ToolError(f"{user!r} is not a known recipient.", known_users=sorted(DM_USERS))
    message = (message or "").strip()
    if not message:
        raise ToolError("Nothing to send.")
    if len(message) > MAX_LEN:
        raise ToolError(
            f"Message is {len(message)} characters; Discord allows {MAX_LEN}. "
            "Shorten it - this tool does not truncate silently.",
            length=len(message), limit=MAX_LEN,
        )

    _, channel = http(f"{API}/users/@me/channels", payload={"recipient_id": user_id}, method="POST")
    channel_id = channel.get("id")
    if not channel_id:
        raise ToolError(f"Discord did not hand back a DM channel id for {user!r}.", response=channel)

    http(f"{API}/channels/{channel_id}/messages", payload={"content": message}, method="POST")

    return {"ok": True, "summary": f"DM sent to {user}: {message[:120]}", "user": user}


def banner():
    return (f"DISCORD_BOT_TOKEN={'set' if BOT_TOKEN else 'MISSING'}  "
            f"DISCORD_DM_USERS={', '.join(sorted(DM_USERS)) or 'MISSING'}")


if __name__ == "__main__":
    run("discord-mcp", "1.0", banner)
