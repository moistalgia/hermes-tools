# discord-mcp

One tool: DM a named person. Nothing else.

No dependencies — `urllib` and the standard library. The protocol half lives
in [../mcpkit.py](../mcpkit.py).

## Why this exists

Gladys (Hermes' Discord gateway) replies wherever a conversation happened —
that's correct for a conversation. It falls down for anything scheduled: a
cron job that delivers to Discord posts its final response to the configured
**home channel**, which the whole household shares. A daily brief, or
anything else meant for one person, has no way to reach that person
specifically without also becoming the channel's business.

This server is the patch for that gap — a single DM tool the agent can call
when a message is for one person, not the room.

**Bot-to-bot DMs are blocked by Discord itself** (error 50007, platform-level
— no permission or intent fixes it). **Bot-to-*user* DMs are not.** This
targets a person, which has always been ordinary for a bot to do — no
different from any moderation bot's DM.

## Setup

**Reuse Gladys' existing bot token.** This is one more recipient of a bot
that's already running, not a second bot to create and invite. Find the token
value Hermes' own Discord integration already uses (in `~/.hermes/.env` or
wherever Hermes keeps it) and copy the *value* into this server's config —
servers here don't read `.env` files, so it has to be a literal in
`config.yaml` (see [Wire into Hermes](#wire-into-hermes)). No new Discord
application, no new intents: opening a DM channel and posting to it needs
nothing beyond what the bot already has.

**Get the recipients' user ids.** In Discord: User Settings → Advanced →
turn on Developer Mode. Then right-click a person → Copy User ID. Do this for
everyone who should be reachable — for a household, that's usually two
people.

**The bot needs to share a server with each recipient**, and each recipient
needs "Allow direct messages from server members" on for that server (the
default). Gladys already meets the first condition for anyone who talks to
her. If a DM fails with "DMs from server members turned off", that's a
setting on the recipient's side, not something this server can work around.

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | *(required)* | Same value Gladys already runs on. |
| `DISCORD_DM_USERS` | *(required)* | `name=id,name=id`. Lowercased on read, so `Nathan` and `nathan` are the same key. |
| `DISCORD_TIMEOUT` | `15` | Seconds. |

`discord_status` prints exactly which of these are set and who is reachable.

Recipients are a **closed, named list** — the agent says `user="nathan"`,
never a raw eighteen-digit id. Same reasoning DESIGN.md gives for rooms and
Plex players: an identifier the agent never sees is one it can't get wrong,
and a name outlives the id the way a room name outlives the bulb in it.

## Tools

| Need | Tool |
| --- | --- |
| Is the channel working, and who can be reached? | `discord_status` |
| Message one person | `discord_dm` |

`discord_dm` opens (or re-opens — idempotent on Discord's side) a DM channel
with the recipient, then posts to it. Messages over 2000 characters are
refused rather than silently truncated; Discord's own limit on a message
body.

## Behaviour worth knowing

**This is outbound only.** Replies to a DM the bot sends land back through
Gladys' normal message handling, same as any other message to the bot — there
is no separate inbound path here to duplicate that.

**No read-back.** Unlike the Home Assistant tools in this repo, there's no
state to poll after sending — Discord's API either accepts the message
(`200`) or explains why not (`403`/`50007` for DMs off, `401` for a bad
token, `429` for rate limiting). Each of those comes back as a `ToolError`
naming the cause, not a bare status code.

## Manual test sequence

```bash
export DISCORD_BOT_TOKEN=<Gladys' existing token>
```

```bash
export DISCORD_DM_USERS=nathan=123456789012345678
```

```bash
python discord_mcp_server.py discord_status
```

Should report the token configured and `nathan` as a known recipient.

```bash
python discord_mcp_server.py discord_dm user=nathan message="if you can read this, the DM channel works"
```

## Wire into Hermes

```yaml
mcp_servers:
  discord:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/discord-mcp/discord_mcp_server.py", "serve"]
    env:
      DISCORD_BOT_TOKEN: "<same token Gladys runs on>"
      DISCORD_DM_USERS: "nathan=<id>,anna=<id>"
```

The skill that drives this server is
[daily-brief](../skills/daily-brief/SKILL.md) — it now sends the brief here
instead of to the home channel. If you also have `notify-mcp` configured,
that remains the channel for anything that wants a reply back (shopping
items texted in from the shop); this one is send-only, for the cases where
the point is specifically *not* posting somewhere shared.
