# walmart-mcp

Select a store, search, and build a cart on Walmart.com — through a handful
of narrow tools that return compact JSON, never a DOM — then hand the cart
off, in the same browser window, for you to log in and check out yourself.

## This is a deliberate exception to DESIGN.md

[DESIGN.md](../DESIGN.md) §8 lists "Automated ordering" as deliberately not
built: *"The demo everyone asks for. Brittle scraping, real money, low
trust."* That reasoning still holds everywhere else in this repo, and
DESIGN.md was not edited to say otherwise — it stays a correct general rule
of thumb. This server exists anyway, and it lands closer to that original
caution than it might look: **it never places an order, at all.** It builds
a cart, then hands the window to a human for the purchase. See "The checkout
gate" below for the mechanics.

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
package. Nothing else in this repo needs this step. `playwright-stealth`
installs alongside `playwright` automatically (it's a listed dependency) —
it's load-bearing, not optional either, see below.

That's it — no separate login script to run. **No tool ever scripts a
login**, on purpose: see the module docstring in `walmart_mcp_server.py` for
why (a guest session sidesteps Walmart's login-specific bot detection, and
it turns out to be a stronger safety property besides — the human is
physically the one who logs in and spends the money, not a token proving an
agent got permission to). That said, this is **not** a credential-free
server in the way that phrasing might imply — read "One browser, one
profile" below before assuming nothing here ever touches your login.

### 2. Hermes

```yaml
  walmart:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/walmart-mcp/walmart_mcp_server.py", "serve"]
```

| Var | Default | Purpose |
| --- | --- | --- |
| `WALMART_PROFILE_DIR` | `~/.hermes/walmart_profile` | Persistent Chrome profile — see below |
| `WALMART_STORE_ID` | unset | Optional saved default store |
| `WALMART_TIMEOUT` | `20` | Page-action timeout, seconds |
| `WALMART_CONFIRM_TIMEOUT` | `15` | Cart read-back poll budget, seconds |
| `WALMART_CHECKOUT_TOKEN_TTL` | `300` | Checkout approval token lifetime, seconds |

The host running this needs a real display. Confirmed by direct testing:
Walmart's bot-check blocks a headless session outright, on the very first
navigation, regardless of stealth patches or a real Chrome binary — only
switching to a visible (headful) browser fixed it. So this server opens one
visible browser window and keeps it open for the life of the process; that's
not configurable, and there's no headless mode to opt into.

### One browser, one profile

This server runs on a **persistent** Chrome profile at `WALMART_PROFILE_DIR`,
not a fresh throwaway one per run. That's also confirmed by direct testing,
not a guess: a brand-new, history-less browser gets challenged by Walmart's
bot-check noticeably more than a normal browser with real visit history
behind it, even with an identical fingerprint otherwise. So cookies and
browsing history accumulate across restarts the same way an ordinary
returning visitor's browser would.

**The practical consequence: after the first time you actually complete a
checkout, this profile holds your real, logged-in Walmart session — on
disk, indefinitely, across restarts, not just for one run.** That was a
deliberate choice (see the module docstring's "One browser, headful,
persistent, and shared with checkout") over keeping automation and login on
two separate profiles: simpler, and a logged-in profile is if anything
*more* trusted by Walmart for the plain browsing this server does the rest
of the time. It does not weaken anything upstream of it — `open_checkout`
still requires a fresh, matching token regardless of whether the browser
happens to be logged in; being authenticated changes what you see when you
take over the window, not what any tool here can do unattended.

To reset to a clean, logged-out profile (e.g. you no longer want a Walmart
session sitting on this disk), delete the `WALMART_PROFILE_DIR` folder.
Expect more frequent bot-checks for a while afterward until it re-ages.

**Optional: give it a head start.** A brand-new profile earns trust through
real use over time, same as any new browser install — but since it's
already sharing state with checkout, logging in yourself once is a shortcut
to the single strongest trust signal available, rather than waiting for that
to happen organically. Run:

```bash
python warm_profile.py
```

Opens the exact same profile/browser/stealth setup this server uses, headful,
and leaves it open for you to browse or log in by hand — not an MCP tool,
never agent-callable, and not required. Press Enter in the terminal when
you're done; there's no separate save step, the profile directory itself is
the persistence.

### 3. Prove it

```bash
python walmart_mcp_server.py find_stores location=<your zip>
```

A visible Chromium window will open (this is normal — see above) and the
call should return a list of stores. Every DOM selector in this server was
driven and confirmed against the live site while it was built — search,
store finder, and cart add/view/update/remove all work as a plain guest, no
login required. Two corrections came out of that process worth knowing: the
pickup/delivery toggle only works from the cart page, not the store finder
or homepage (`select_store` routes through `/cart` because of this), and
"Continue to checkout" opens a delivery slot picker before the actual
checkout page, not a payment page directly.

If `find_stores` returns stores, the session and the store-finder selectors
both work end to end. If it fails with a "layout may have changed" error,
the selectors in `walmart_mcp_server.py` need adjusting against the real
page — they're centralized under the "DOM-touching helpers" comment
specifically so that's a local edit, not a hunt.

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
| `open_checkout` | Brings the cart back on screen for the user to log in and buy it themselves. Requires a valid token from `checkout_preview`. Never places an order. |

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
- **Is single-use** — spent the moment `open_checkout` accepts it.
- **Lives only in this process's memory.** A server restart between preview
  and handoff invalidates every pending token. That's intentional — a
  restart is exactly the kind of discontinuity that should force
  re-confirmation.

`open_checkout` checks, in order: a token was passed at all, it's one this
process actually issued, it hasn't been used, it hasn't expired, and the
live cart still matches what was previewed. Any failure is a specific,
named `ToolError` — and even on success, all it does is bring the existing
browser window back to `/cart` and stop. It never clicks anything past that.

**This alone does not guarantee a human approved the checkout** — nothing
stops a careless caller from calling `checkout_preview` then `open_checkout`
back-to-back without showing anyone the summary. That half is policy, not
mechanism, and lives in
[skills/walmart-shopping/SKILL.md](../skills/walmart-shopping/SKILL.md): the
skill instructs that `open_checkout` is never called without pasting the
`checkout_preview` summary into the conversation and getting an explicit yes
first. The token stops accidental/stale/replayed calls; the skill is what
puts a person in the loop before the window even opens — and the human is
the one who has to log in and click "Place order" regardless, which is the
part that actually spends money.

## Known failure modes

- **Out of stock at search time** — reported as `in_stock: false`, not left
  out of the results (an omitted item looks like it doesn't exist).
- **Item not fulfillable at the active store/method** — `add_to_cart` names
  what *is* available rather than failing bare.
- **Price changed between search and add** — `add_to_cart`'s summary states
  the real price that landed, not the one that was searched.
- **Price or stock changed between preview and handoff** — caught by the
  cart-signature check; `open_checkout` fails and asks for a fresh preview.
- **A "Robot or human? Press & Hold" challenge appears mid-action** —
  confirmed live, most likely on `add_to_cart`. This server never tries to
  solve it — simulating a hold gesture to defeat a human-verification check
  is a different, more aggressive thing than just not looking automated by
  accident, and it isn't something built here even for the account owner's
  own shopping. The tool fails with `needs_human: true` and names exactly
  what to do: check the visible browser window, hold the button for a
  couple of seconds, then call the same tool again.
- **Walmart changes their layout** — a selector miss is reported as "needs a
  selector fix, not a retry," not retried with altered arguments.

## What this doesn't do

- **No `cancel_order`.** There is no `place_order` either — the human places
  it, in their own browser session, after logging in themselves.
- **No browsing by category, no reordering past purchases, no multi-store
  cart merging.** The surface is curated to exactly: select store → search →
  cart → verify → hand off.
- **No `login` tool.** Ever — no tool call, at any point, scripts entering a
  password. The profile can still end up holding a real logged-in session
  after a completed checkout, though — see "One browser, one profile" in
  Setup. That's the human logging in by hand, not this server doing it.
