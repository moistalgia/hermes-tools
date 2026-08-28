---
name: walmart-shopping
description: Select a Walmart store, search for items, build a cart, and hand it off for checkout. Use whenever someone asks to order groceries or other items from Walmart, add something to a Walmart cart, or check out a Walmart order. All of it goes through the `walmart` MCP server — never through a general browser tool. The server never places an order itself — it opens a checkout window and the user finishes it themselves.
tags: []
related_skills: []
---

# Walmart Shopping

All of it goes through the **`walmart` MCP server**. Never drive Walmart.com
with a general browser/UI tool for this. That's not a style preference — it's
how a single stuck retry loop (an address-autocomplete field) burned most of
a session's context in one attempt at exactly this task. The `walmart` server
exists specifically so this never happens again: it returns a few structured
fields per call instead of a page's worth of DOM. **If a `walmart` tool
fails, the fix is never a different transport.** Report the error and stop —
don't fall back to a browser tool to work around it.

`walmart` never scripts a login and never places an order — no tool up
through `checkout_preview` enters a password, ever. (After the user's first
real checkout, the browser profile it runs in does stay logged in on disk —
that's the human's own login persisting, not this server acquiring one.
Doesn't change anything here: `open_checkout` still only brings the cart up
on screen for the user to buy it themselves.) Nothing in this server ever
spends money on its own.

## The flow

```
find_stores → select_store → search_items → add_to_cart (repeat) →
view_cart → checkout_preview → [ask the user] → open_checkout →
tell the user it's ready
```

`find_stores` and `select_store` only need to happen once per session unless
the store or fulfillment method changes.

## Opening checkout requires the user's explicit yes — every time

This is the one rule in this skill that matters more than the others.
`checkout_preview` is read-only and mints a token; it does not order
anything, and neither does `open_checkout` — it just brings the browser
window to the cart. **Never call `open_checkout` without first pasting the
`checkout_preview` summary — items, quantities, total, fulfillment — into the
conversation and getting an explicit yes from the user in that same turn.**

This isn't enforced by the server alone. `walmart-mcp`'s checkout token stops
accidental, stale, or replayed calls (see
[walmart-mcp/README.md](../../walmart-mcp/README.md#the-checkout-gate)), but
nothing there stops calling `checkout_preview` and `open_checkout`
back-to-back without ever showing anyone the total. That's this skill's job.
"Yes, order it" after seeing the total is a yes. A request to "add some
groceries" is not — that's permission to build the cart, not to buy it.

If the user doesn't respond in the same turn, or gives an ambiguous answer,
stop and ask again. Do not treat silence, a topic change, or "sounds good"
about something else as approval.

## Building the cart

**Confirm items before adding when the request is vague.** "Get milk" could
mean a dozen different products at a dozen prices — `search_items` and show
the top few if there's real ambiguity in brand, size, or price. "Get the
usual milk" or a specific product name doesn't need that back-and-forth.

**Report what `add_to_cart` actually confirmed, not what was requested.** Its
read-back is the truth — if the price changed or the quantity landed
differently (some items cap quantity), say so rather than restating the
number that was asked for.

**Out-of-stock items are reported, not silently skipped.** If something in a
list isn't available, say which one and either suggest an alternative or ask
whether to proceed without it — don't drop it from the cart quietly.

## When a tool reports `needs_human: true`

Walmart occasionally puts up a "Robot or human? Press & Hold" challenge in
the browser window, most likely right after an `add_to_cart` call. When that
happens the tool fails on purpose rather than retrying — it will never try
to click through this itself. Tell the user plainly: *"Walmart wants a quick
human check — could you press and hold the button in the browser window for
a couple of seconds?"* Then call the exact same tool again once they say
they've done it. This is not a bug and not something to route around with a
different tool or a loop of retries — it is a person actually confirming
they're a person, which nothing here can or should fake.

## At checkout

Read the `checkout_preview` summary back to the user in plain terms — items,
quantities, total, and whether it's pickup or delivery. Don't just say "ready
to check out" and wait; give them the actual numbers to say yes to.

Once `open_checkout` succeeds, the job here is done — say so plainly: *"The
checkout window is ready with your cart. Go ahead and log in there to finish
it — I can't see whether you complete it."* Do not imply the order is placed,
in progress, or confirmed. This tool never learns the outcome, and there is
nothing to poll or check afterward — the next signal about this order, if
any, comes from the user telling you directly.

If `open_checkout` fails because the cart changed since the preview (price or
stock shifted), call `checkout_preview` again and show the *new* total —
don't assume the old number still applies.

## What not to do

**Never call `open_checkout` speculatively** — not to "see what happens," not
because the cart looks obviously final, not because the user said yes to
something else earlier in the conversation. Every checkout hand-off needs its
own yes, after its own summary.

**Don't retry a failed `walmart` tool call by falling back to a browser.**
See the top of this file — that's the exact failure this server was built to
avoid.

**Don't build a cart across multiple stores.** `select_store` is single-store
by design; if items aren't available at the active store, say so rather than
trying to route around it.

**Don't try to solve a `needs_human` challenge yourself, in any way.** No
retries, no alternate tool, no clever selector. See above — this is the one
place in the whole flow that is supposed to stop and wait for a person.
