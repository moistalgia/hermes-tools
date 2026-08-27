# walmart-mcp

Select a store, search, manage a cart, and check out on Walmart.com — through
a handful of narrow tools that return compact JSON, never a DOM.

## This is a deliberate exception to DESIGN.md

[DESIGN.md](../DESIGN.md) §8 lists "Automated ordering" as deliberately not
built: *"The demo everyone asks for. Brittle scraping, real money, low
trust."* That reasoning still holds everywhere else in this repo, and
DESIGN.md was not edited to say otherwise — it stays a correct general rule
of thumb. This server exists anyway because the convenience was judged worth
it for one narrow flow, on the explicit condition that **no order places
itself.** See "The checkout gate" below for how that's enforced — as a
property of the tool surface, not a prompt.

This server also exists because of a concrete failure: Hermes was driving
Walmart.com through a generic browser-automation tool, and it blew out a 64k
context window in one session — every raw DOM read it got back was resent on
every later turn, and a single stuck address-autocomplete field alone ate a
large share of it. The fix here isn't a bigger context window. It's that the
DOM never leaves this process; every tool returns a few structured fields
instead.

## Setup

### 1. Install

```bash
uv pip install -e .
playwright install chromium
```

The second command is not optional — it fetches a browser binary, not a
package. Nothing else in this repo needs this step.

### 2. Log in once, by hand

```bash
python auth_setup.py
```

Opens a real, visible browser window. Log in — including any 2FA prompt —
then press Enter in the terminal. This saves cookies/localStorage to
`WALMART_STORAGE_STATE` (default `~/.hermes/walmart_state.json`). No tool in
this server can do this step itself, on purpose — see `auth_setup.py`'s own
docstring for why. Re-run it whenever a tool reports the session expired.

### 3. Hermes

```yaml
  walmart:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/walmart-mcp/walmart_mcp_server.py", "serve"]
    env:
      WALMART_STORAGE_STATE: "C:/Users/<you>/.hermes/walmart_state.json"
      WALMART_HEADLESS: "1"
```

| Var | Default | Purpose |
| --- | --- | --- |
| `WALMART_STORAGE_STATE` | `~/.hermes/walmart_state.json` | Session cookies from `auth_setup.py` |
| `WALMART_HEADLESS` | `1` | Always headless for the agent-facing server |
| `WALMART_STORE_ID` | unset | Optional saved default store |
| `WALMART_TIMEOUT` | `20` | Page-action timeout, seconds |
| `WALMART_CONFIRM_TIMEOUT` | `15` | Cart read-back poll budget, seconds |
| `WALMART_CHECKOUT_TOKEN_TTL` | `300` | Checkout approval token lifetime, seconds |

### 4. Prove it

```bash
python walmart_mcp_server.py find_stores location=<your zip>
```

Every DOM selector in this server was driven and confirmed against the live
site while it was built — as a guest for search/cart/store-finder, and
against a real logged-in account for the fulfillment toggle and checkout.
Two corrections came out of that: the pickup/delivery toggle only works from
the cart page, not the store finder or homepage (`select_store` routes
through `/cart` because of this), and "Continue to checkout" opens a delivery
slot picker before checkout proper, not a payment page directly. The real
checkout was driven all the way to a page titled "Review your order" with a
button whose exact accessible name is `"Place order for $19.64"` (the real
total) — confirmed, screenshotted, and never clicked. The only thing left
unverified is the order-confirmation page itself, which can't be reached
without actually placing an order — see `submit_order`'s docstring in
`walmart_mcp_server.py` if `place_order` ever reports a "layout may have
changed" error past that point.

If `find_stores` returns stores, the session and the store-finder selectors
both work end to end. If it fails with a "layout may have changed" error, the
selectors in `walmart_mcp_server.py` need adjusting against the real page —
they're
centralized under the "DOM-touching helpers" comment specifically so that's a
local edit, not a hunt.

## Tools

| Tool | Does |
| --- | --- |
| `find_stores` | Zip/address → nearby stores with pickup/delivery availability. Call first — every other tool needs a `store_id`. |
| `select_store` | Sets the active store + fulfillment method. |
| `search_items` | Item search at the active store. Out-of-stock items are returned, not omitted. |
| `add_to_cart` | Adds an item, then reads the cart back to confirm it actually landed. |
| `view_cart` | The live cart: items, quantities, prices, subtotal. |
| `update_cart` | Change a quantity. `quantity=0` removes it. |
| `remove_from_cart` | Take an item off the cart entirely. |
| `checkout_preview` | Read-only cart summary + a confirmation token. Orders nothing. |
| `place_order` | The only tool that spends money. Requires a valid token from `checkout_preview`. |

Every one is also a CLI subcommand through the same dispatch path:

```bash
python walmart_mcp_server.py search_items query="whole milk"
python walmart_mcp_server.py view_cart
```

## An accepted click is not a cart

`add_to_cart` clicking "Add" is not the same as the item being in the cart at
the right quantity and price — so every write here reads the live cart back
before reporting success, the same convention [qbt-mcp](../qbt-mcp/README.md)
uses for downloads. `add_to_cart`'s summary states the price actually landed
in the cart, even when it doesn't match what `search_items` showed — prices
move between the two calls sometimes, and that's reported, never silently
swallowed.

## The checkout gate

`checkout_preview` is read-only: it reads the cart, computes the total, and
mints a `confirmation_token` bound to that *exact* cart — item ids,
quantities, and prices, not just the dollar total (two different carts can
sum to the same number). The token:

- **Expires in 5 minutes** (`WALMART_CHECKOUT_TOKEN_TTL`) — long enough for a
  human to read the summary and reply, short enough that a price or stock
  change during the window is the exception.
- **Is single-use** — spent the moment `place_order` accepts it, before the
  order is actually dispatched, so a crash mid-order can't be replayed into a
  double order.
- **Lives only in this process's memory.** A server restart between preview
  and order invalidates every pending token. That's intentional — a restart
  is exactly the kind of discontinuity that should force re-confirmation.

`place_order` checks, in order: a token was passed at all, it's one this
process actually issued, it hasn't been used, it hasn't expired, and the
live cart still matches what was previewed. Any failure is a specific,
named `ToolError` — never a purchase.

**This alone does not guarantee a human approved the order** — nothing stops
a careless caller from calling `checkout_preview` then `place_order`
back-to-back without showing anyone the summary. That half is policy, not
mechanism, and lives in
[skills/walmart-shopping/SKILL.md](../skills/walmart-shopping/SKILL.md): the
skill instructs that `place_order` is never called without pasting the
`checkout_preview` summary into the conversation and getting an explicit yes
first. The token stops accidental/stale/replayed calls; the skill is what
puts a person in the loop.

## Known failure modes

- **Out of stock at search time** — reported as `in_stock: false`, not left
  out of the results (an omitted item looks like it doesn't exist).
- **Item not fulfillable at the active store/method** — `add_to_cart` names
  what *is* available rather than failing bare.
- **Price changed between search and add** — `add_to_cart`'s summary states
  the real price that landed, not the one that was searched.
- **Price or stock changed between preview and order** — caught by the
  cart-signature check; `place_order` fails and asks for a fresh preview.
- **Session expired mid-task** — every tool fails once, loudly, pointing at
  `auth_setup.py`. None of them retry a login in a loop — that retry pattern
  is what blew out context in the first place.
- **Walmart changes their layout** — a selector miss is reported as "needs a
  selector fix, not a retry," not retried with altered arguments.

## What this doesn't do

- **No `cancel_order`.** Canceling a placed order is a harder, higher-stakes
  operation (refunds, already-picked items) than anything asked for here.
  Its absence is deliberate, not an oversight.
- **No browsing by category, no reordering past purchases, no multi-store
  cart merging.** The surface is curated to exactly: select store → search →
  cart → verify → checkout.
- **No `login` tool.** Ever. See `auth_setup.py`.
