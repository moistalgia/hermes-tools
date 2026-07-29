# notify-mcp

The channel the agent reaches you through when you are not at a keyboard, and
the channel you reach it through from your phone.

No dependencies — `urllib` and the standard library. The protocol half lives in
[../mcpkit.py](../mcpkit.py).

## Why this is not optional

An assistant that only answers is a tool. One that can start the conversation is
something else, and starting a conversation needs somewhere to put the words
that you will actually read. Email is where reminders go to die.

**Build this second, before anything ambitious.** Prove one scheduled job can
push one boring message end to end. That loop — schedule fires, agent runs,
message arrives — is where the breakage is, and finding out it is broken while
you are also debugging a daily brief is twice the work.

## Backends

| | Inbound | Notes |
| --- | --- | --- |
| `telegram` *(default)* | yes, best | Real conversations and replies, and more than one person can message the same bot — which is what makes it work for a household. |
| `ntfy` | yes | No account, self-hostable. The phone app publishes back to the same topic, but it is a text field rather than a conversation. Pick an unguessable topic name; a public ntfy.sh topic is readable by anyone who guesses it. |
| `pushover` | no | Most reliable delivery, one-way only. |

Choose based on inbound. Being able to text "add olive oil to the list" from the
shop is the single largest jump in how much a system like this gets used — it
stops being something you sit down at.

## Setting up Telegram

1. Message `@BotFather` on Telegram, send `/newbot`, and answer its two
   questions. It gives you a token — that is `TELEGRAM_TOKEN`.
2. Set the token and nothing else:

   ```bash
   export TELEGRAM_TOKEN=123456:AA...
   ```

3. Open a chat with your new bot from your phone and send it anything.
4. Ask the server who sent it:

   ```bash
   python notify_mcp_server.py inbox_fetch
   ```

   The message comes back with a `chat_id`. That is `TELEGRAM_CHAT_ID`.

`inbox_fetch` deliberately does **not** require the chat id, which is what makes
step 4 work at all. Everything else does.

Anyone else in the household who messages the bot shows up in `inbox_fetch` with
their own name, so household capture works without further setup. Outbound
`notify` goes to the one configured `TELEGRAM_CHAT_ID` — a group chat id works
there if you want the whole house to see the brief.

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `NOTIFY_BACKEND` | `telegram` | `telegram`, `ntfy`, or `pushover`. |
| `NOTIFY_STATE` | `%USERPROFILE%\.hermes\notify.json` | Inbound read cursor. If it is lost, the next fetch replays old messages and the agent files them a second time. |
| `NOTIFY_TIMEOUT` | `15` | Seconds. |
| `TELEGRAM_TOKEN` | *(required for telegram)* | From `@BotFather`. |
| `TELEGRAM_CHAT_ID` | *(required to send)* | See above. Not needed for `inbox_fetch`. |
| `NTFY_SERVER` | `https://ntfy.sh` | Your own server if you self-host. |
| `NTFY_TOPIC` | *(required for ntfy)* | Treat it as a secret. |
| `NTFY_TOKEN` | — | Only for protected topics. |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | *(required for pushover)* | From the Pushover dashboard. |

`notify_status` prints exactly which of these are set and which are missing for
the selected backend. Every failure names the variable rather than saying "not
configured."

## Tools

| Need | Tool |
| --- | --- |
| Is the channel working? | `notify_status` |
| Tell the user something | `notify` |
| Read what they sent from their phone | `inbox_fetch` |

Priority is four words — `low`, `normal`, `high`, `urgent` — mapped to whatever
each backend numbers them. `urgent` bypasses quiet hours on most phones, so it
is for water, fire, and doors.

## Behaviour worth knowing

**`inbox_fetch` files nothing.** It returns messages and stops there. The skill
decides whether a message is a task, a shopping item, or noise, and writes it
through [state-mcp](../state-mcp/) — usually `capture_add`. Transport and
meaning stay in separate servers; otherwise this quietly becomes a second, worse
state store.

**The read cursor is what stops double-filing.** `inbox_fetch` advances it by
default. Pass `mark_read=false` only when debugging, and expect to see the same
messages again.

**On ntfy, the agent's own posts are filtered out of inbound.** Outbound
notifications land on the same topic they are read from, so without the filter
the agent reads its own messages back and files them as user input — which looks
exactly like a haunting. The filter keys on the `Hermes` title, so leave the
default title alone on outbound messages unless you also change the filter.

Telegram has no such problem: `getUpdates` returns messages sent *to* the bot,
never the ones it sent. One of several reasons it is the default.

## Manual test sequence

Stop at the first failure and read the error; it names the cause.

```bash
export TELEGRAM_TOKEN=123456:AA...
```

```bash
python notify_mcp_server.py notify_status
```

It will report `TELEGRAM_CHAT_ID` missing. That is expected at this point.
Message the bot from your phone, then:

```bash
python notify_mcp_server.py inbox_fetch
```

Your message comes back with its `chat_id`. Set it and send one the other way:

```bash
export TELEGRAM_CHAT_ID=987654321
```

```bash
python notify_mcp_server.py notify message="if you can read this, the channel works" priority=high
```

Then confirm the cursor advanced — a second `inbox_fetch` should return nothing
rather than replaying the message you already read.

## Wire into Hermes

```yaml
mcp_servers:
  notify:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/notify-mcp/notify_mcp_server.py", "serve"]
    env:
      TELEGRAM_TOKEN: "<from @BotFather>"
      TELEGRAM_CHAT_ID: "<from inbox_fetch>"
```

The skills that drive this server are
[daily-brief](../skills/daily-brief/SKILL.md) and
[household-state](../skills/household-state/SKILL.md).
