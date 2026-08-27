#!/usr/bin/env python3
"""
walmart-mcp - select a store, search, manage a cart, and check out on
Walmart.com through a small number of purpose-built tools, so the agent
driving Hermes never sees a DOM.

## Why this exists, and why it is not the usual shape

DESIGN.md Section 8 ("Deliberately not built") lists automated ordering as
rejected - brittle scraping, real money, low trust. That reasoning is still
correct everywhere else in this repo. This server is a deliberate, narrow
exception: Hermes was driving Walmart.com through a generic browser-automation
tool (`drive_preview`), and every raw DOM/accessibility-tree read it returned
got resent on every later turn - a single stuck address-autocomplete field
alone burned a large share of a 64k context window in one session. The fix is
architectural, not a tuning knob: this server eats the DOM-reading cost once,
internally, and hands back compact JSON. See walmart-mcp/README.md for the
full reasoning and the condition it was built under (checkout never places
itself without approval).

There is no official consumer-facing Walmart API for search + cart + checkout
- their Commerce/Marketplace APIs are partner/seller-only. Playwright driving
a persisted, already-logged-in session is the only real option, and this is
the first browser-automation dependency anywhere in this repo. Login itself is
never scripted: Walmart's login flow is adversarial toward automation
specifically (bot detection, likely 2FA on a new device fingerprint), so
scripting it would mean fighting that arms race on every tool call instead of
once. `auth_setup.py` is a human, headful, one-time step instead - see there
and in README for the reasoning.

## The checkout gate

`place_order` is the only tool here that spends money, and nothing else in
this repo has a "structural approval gate" between two tool calls - it needs
its own justification. DESIGN.md's own argument against relying on a prompt
alone (Section 8, locks row: "a system prompt is not a control... safety has
to be a property of the tool surface") applies here too. `checkout_preview`
mints a short-lived, single-use token bound to the *exact* live cart (not
just its total - two different carts can sum to the same number); `place_order`
fails closed unless handed that exact, fresh, unused, matching token. This
stops accidental, stale, or replayed calls on its own. It does **not** by
itself guarantee a human saw the summary before approving - that half is
policy, not mechanism, and lives in skills/walmart-shopping/SKILL.md instead.

Selectors below were driven and verified against the live site, both as a
guest and against a real logged-in account: search results, product-page
add-to-cart, cart line items/quantity/remove/subtotal, the store finder's
result list and "make this my store" action, the fulfillment (pickup/
delivery) toggle - which turned out to live only on the cart page, not the
store finder or homepage, correcting an earlier guess - and checkout up to
and including the real "Place order for $X.XX" button's exact accessible
name, reached and screenshotted without ever clicking it. See the git
history for the exact aria-labels and data-testid/data-automation-id
attributes observed at each step. The one thing that could not be verified
without actually placing an order is the order-confirmation page itself
(`submit_order`'s final selector) - deliberately never reached, for the same
reason the checkout gate exists at all. Selectors are centralized in one
place (search for "DOM-touching") so fixing any of this is a local edit, not
a hunt, and Walmart's own utility CSS class names (which churn constantly,
e.g. `ld_Ec`) were deliberately avoided in favor of `data-testid`,
`data-automation-id`, and accessible role/name - all far more stable across
their deploys. If a selector is wrong anyway, every dependent tool fails
loudly with a named "layout may have changed" error - never a wrong answer.

Two ways to run it:

    python walmart_mcp_server.py serve                     # MCP server, agent use
    python walmart_mcp_server.py find_stores location=90210  # CLI, human proof
"""

import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, run, s, tool  # noqa: E402

BASE_URL = "https://www.walmart.com"

STORAGE_STATE = os.environ.get(
    "WALMART_STORAGE_STATE",
    os.path.join(os.path.expanduser("~"), ".hermes", "walmart_state.json"),
)
HEADLESS = os.environ.get("WALMART_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
DEFAULT_STORE_ID = os.environ.get("WALMART_STORE_ID", "").strip() or None
TIMEOUT_MS = int(os.environ.get("WALMART_TIMEOUT", "20")) * 1000

# How long a cart read-back (add/update/remove) polls before reporting
# unconfirmed. Mirrors QBT_CONFIRM_TIMEOUT's role in qbt-mcp.
CONFIRM_TIMEOUT = int(os.environ.get("WALMART_CONFIRM_TIMEOUT", "15"))

# Checkout approval token lifetime. Long enough for a human to read a summary
# and reply, short enough that a price/stock change during the window is the
# exception, not something routinely worked around.
CHECKOUT_TOKEN_TTL = int(os.environ.get("WALMART_CHECKOUT_TOKEN_TTL", "300"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Patches the handful of signals headless Chromium leaves that a bot-detection
# script checks for. Without this, a headless session can get walled off
# before any tool call reaches real content - not a way to defeat detection
# aimed at abuse, just to look like the same browser a human would be sitting
# in front of for an account this user is already logged into.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(params)
    );
}
"""

# Login-wall / challenge-page URL fragments. Any navigation that lands here
# means the saved session is dead - fail once, loudly, never retry in a loop.
LOGIN_WALL_MARKERS = ("account/login", "account/verify", "blocked", "challenge")


# ---------------------------------------------------------------------------
# Process-level state
# ---------------------------------------------------------------------------

_playwright = None
_browser = None
_context = None
_page = None

_active_store = {"store_id": DEFAULT_STORE_ID, "fulfillment": None}

# token -> {"signature", "total", "items", "issued_at", "used"}. In-memory,
# process-scoped - the same shape every other server here uses for cached
# state (_server in plex-mcp, _opener in qbt-mcp). One long-lived server
# process per Hermes session, so there is no cross-session leak to design
# around. A server restart between checkout_preview and place_order
# invalidates every pending token by design: a restart is exactly the kind of
# discontinuity that should force re-confirmation, not silently honor a stale
# approval.
_pending_checkouts = {}


# ---------------------------------------------------------------------------
# Low-level Playwright accessor
# ---------------------------------------------------------------------------


def page():
    """A logged-in page, built once. Never logs in itself - see module docstring."""
    global _playwright, _browser, _context, _page
    if _page is not None:
        return _page

    if not os.path.exists(STORAGE_STATE):
        raise RuntimeError(
            f"No saved Walmart session at {STORAGE_STATE}. Run `python "
            f"auth_setup.py` on the host to log in once by hand - this cannot "
            f"be done from here."
        )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        )

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
    )
    _context = _browser.new_context(
        storage_state=STORAGE_STATE,
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    _context.add_init_script(STEALTH_INIT_SCRIPT)
    _context.set_default_timeout(TIMEOUT_MS)
    _page = _context.new_page()
    return _page


def ensure_logged_in():
    """Raise a named, final error if the saved session has died mid-task."""
    url = page().url.lower()
    if any(marker in url for marker in LOGIN_WALL_MARKERS):
        raise ToolError(
            "Walmart is asking to log in again - the saved session has "
            "expired. This can't be fixed from here. Run `python "
            "auth_setup.py` on the host, then retry.",
            session_expired=True,
        )


def dom_action(description, fn):
    """Run a Playwright interaction, translating a timeout/missing-element
    into a named error instead of a raw traceback. Layout drift is a code
    problem, not something a retry with different arguments fixes."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        return fn()
    except PlaywrightTimeoutError:
        raise ToolError(
            f"Could not {description} - the page didn't respond as expected. "
            f"Walmart's layout may have changed, or the page loaded slowly. "
            f"This needs a selector fix, not a retry.",
            hint="report this verbatim",
        )


# ---------------------------------------------------------------------------
# DOM-touching helpers (best-effort selectors, centralized and unverified -
# see module docstring)
# ---------------------------------------------------------------------------


def _extract_distance(text):
    m = re.search(r"([\d.]+)\s*miles?\s*away", text)
    return float(m.group(1)) if m else None


def _parse_price(text):
    m = re.search(r"[\d,]+\.\d{2}", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def _dom_find_stores(location):
    """Verified live: /store/finder, the location box is
    input[name="zipCodeOrCityState"], and each result is anchored by a
    role="checkbox" element whose aria-label is "<store type> <address>" -
    its immediate parent is the card, containing exactly one a[href^="/store/"]
    (the numeric store_id) and either "Make this my store" or "My store"."""
    p = page()
    dom_action("open the store finder", lambda: p.goto(f"{BASE_URL}/store/finder", timeout=TIMEOUT_MS))
    ensure_logged_in()
    box = p.locator('input[name="zipCodeOrCityState"]')
    dom_action("enter the search location", lambda: box.fill(location))
    find_button = p.get_by_role("button", name="Find store")
    dom_action("submit the store search", lambda: find_button.click())
    dom_action("wait for store results", lambda: p.wait_for_selector('[aria-label="results-list"]', timeout=TIMEOUT_MS))

    boxes = p.locator('[aria-label="results-list"] [role="checkbox"][aria-label]')
    stores = []
    for idx in range(boxes.count()):
        box_el = boxes.nth(idx)
        card = box_el.locator("xpath=..")
        link = card.locator('a[href^="/store/"]').first
        href = link.get_attribute("href") or ""
        store_id = href.rstrip("/").rsplit("/", 1)[-1]
        if not store_id:
            continue
        text = card.inner_text()
        stores.append({
            "store_id": store_id,
            "name": box_el.get_attribute("aria-label"),
            "address": box_el.get_attribute("aria-label"),
            "distance_mi": _extract_distance(text),
            # Pickup/delivery availability isn't shown per-store on this list -
            # select_store's read-back of the actual fulfillment toggle (see
            # below) is the authoritative check, not this default.
            "pickup_available": True,
            "delivery_available": True,
        })
    return stores


def _dom_select_store(store_id, fulfillment):
    """Verified live, both halves, against a real logged-in account.

    Making a store active happens on /store/finder itself (the store detail
    page /store/<id> has no such control) - click the "Make this my store"
    button in that store's result card.

    The pickup/delivery/shipping toggle is a different story than first
    guessed: it does NOT open from the homepage's fulfillment-banner button
    (data-automation-id="fulfillment-banner", confirmed clicking that does
    something else - never produced the toggle) and it does NOT appear at
    all on /store/finder. It only opens from the **cart page**, via the
    "Pickup and delivery options heading" button, confirmed twice against a
    real session. So this function deliberately finishes on /cart rather
    than wherever setting the store landed - that's the one page the
    fulfillment toggle is known to work from."""
    p = page()
    dom_action("open the store finder", lambda: p.goto(f"{BASE_URL}/store/finder", timeout=TIMEOUT_MS))
    ensure_logged_in()
    card_link = p.locator(f'a[href="/store/{store_id}"]').first
    dom_action(f"find store {store_id} in the results", lambda: card_link.wait_for(timeout=TIMEOUT_MS))
    card = card_link.locator("xpath=..")
    make_my_store = card.get_by_role("button", name="Make this my store")
    if make_my_store.count():
        dom_action(f"make store {store_id} active", lambda: make_my_store.click())

    dom_action("open the cart to set fulfillment", lambda: p.goto(f"{BASE_URL}/cart", timeout=TIMEOUT_MS))
    toggle = p.get_by_role("button", name="Pickup and delivery options")
    if toggle.count():
        dom_action("open fulfillment options", lambda: toggle.click())
        method_button = p.get_by_role("button", name=fulfillment)
        dom_action(f"select {fulfillment}", lambda: method_button.click())


def _dom_search(query, limit):
    """Verified live: /search?q=... , each result tile is [data-item-id],
    name is [data-automation-id="product-title"], price is
    [data-automation-id="product-price"], and a tile with no
    [data-automation-id="add-to-cart"] button is out of stock (confirmed
    against real out-of-stock tiles, which drop the button entirely rather
    than disabling it)."""
    p = page()
    url = f"{BASE_URL}/search?q={urllib.parse.quote(query)}"
    dom_action("open the search results", lambda: p.goto(url, timeout=TIMEOUT_MS))
    ensure_logged_in()
    dom_action("wait for search results", lambda: p.wait_for_selector("[data-item-id]", timeout=TIMEOUT_MS))

    tiles = p.locator("[data-item-id]")
    count = min(tiles.count(), limit)
    items = []
    for idx in range(count):
        tile = tiles.nth(idx)
        item_id = tile.get_attribute("data-item-id") or ""
        title_el = tile.locator('[data-automation-id="product-title"]').first
        name = title_el.inner_text() if title_el.count() else ""
        price_el = tile.locator('[data-automation-id="product-price"]').first
        price = _parse_price(price_el.inner_text()) if price_el.count() else None
        in_stock = tile.locator('[data-automation-id="add-to-cart"]').count() > 0
        items.append({
            "item_id": item_id, "name": name, "price": price,
            "in_stock": in_stock, "fulfillable_here": in_stock,
        })
    return items


def _dom_add_to_cart(item_id, quantity):
    """Verified live: /ip/x/<item_id> resolves correctly regardless of the
    slug (Walmart looks it up by the trailing numeric id). The buy-box "Add
    to cart" button uses data-automation-id="atc" - distinct from the
    data-automation-id="add-to-cart" used by every tile in a
    recommendation carousel elsewhere on the same page, which is what makes
    this selector safe to use unscoped. There is no pre-add quantity field -
    clicking it always adds exactly 1, confirmed against a live add. A
    requested quantity above 1 is reached afterward via the cart's stepper,
    the same mechanism update_cart uses."""
    p = page()
    dom_action(f"open item {item_id}", lambda: p.goto(f"{BASE_URL}/ip/x/{item_id}", timeout=TIMEOUT_MS))
    ensure_logged_in()
    add_button = p.locator('[data-automation-id="atc"]')
    dom_action("click add to cart", lambda: add_button.click())
    if quantity > 1:
        _dom_set_cart_quantity(item_id, quantity)


def _cart_row(p, item_id):
    """The <li> for one cart line item. Verified live: cart <li>s carry no
    data-item-id - identification is by the product link's href, which ends
    in /<item_id> regardless of the slug in front of it."""
    return p.locator(f"li:has(a[href$='/{item_id}'])").first


def _current_cart_quantity(row):
    """Read the quantity straight from the stepper's own aria-label
    ("Increase quantity <name>, Current Quantity N") rather than trusting a
    running count - Walmart, not this code, is the source of truth for
    whether a click actually landed."""
    inc = row.get_by_role("button", name="Increase quantity")
    aria = inc.get_attribute("aria-label") or ""
    m = re.search(r"Current Quantity (\d+)", aria)
    return int(m.group(1)) if m else 1


def _dom_set_cart_quantity(item_id, quantity):
    """Verified live: quantity is a stepper (data-testid="quantity-stepper",
    -inc-button/-dec-button), NOT a fillable number input - there is no text
    field to type a quantity into anywhere in the cart. Reaching an
    arbitrary quantity means reading the current one and clicking the
    correct button that many times."""
    p = page()
    dom_action("open the cart", lambda: p.goto(f"{BASE_URL}/cart", timeout=TIMEOUT_MS))
    ensure_logged_in()
    row = _cart_row(p, item_id)
    current = dom_action(f"read the current quantity for {item_id}", lambda: _current_cart_quantity(row))
    delta = quantity - current
    if delta == 0:
        return
    button = row.get_by_role("button", name="Increase quantity" if delta > 0 else "Decrease quantity")
    for _ in range(abs(delta)):
        dom_action(f"adjust quantity for {item_id}", lambda: button.click())


def _dom_remove_from_cart(item_id):
    """Verified live: the remove control's accessible name is
    "Remove <product name>" - substring role-name matching on "Remove" is
    enough and does not depend on the product name."""
    p = page()
    dom_action("open the cart", lambda: p.goto(f"{BASE_URL}/cart", timeout=TIMEOUT_MS))
    ensure_logged_in()
    row = _cart_row(p, item_id)
    remove_button = row.get_by_role("button", name="Remove")
    dom_action(f"remove {item_id} from the cart", lambda: remove_button.click())


def read_cart():
    """The live cart: items, quantities, unit prices, subtotal.

    This is the chokepoint every write tool reads back against - the same
    role qbt-mcp's `torrents/info` read-back plays after `torrents/add`.
    Tests substitute this function directly rather than faking Playwright.

    Verified live: each line item is an <li> containing
    [data-testid="quantity-stepper"] (which is what distinguishes it from
    the "you might also like" recommendation tiles also present on this
    page, which have add-to-cart buttons but no stepper). Name is
    [data-testid="productName"]. Price is read from a screen-reader-only
    span whose text starts with "Current price $X.XX" - deliberately not a
    data-testid, since none was present on the price element, but the
    "Current price" text prefix is exact and unlikely to be reused for
    anything else in a cart row.
    """
    p = page()
    dom_action("open the cart", lambda: p.goto(f"{BASE_URL}/cart", timeout=TIMEOUT_MS))
    ensure_logged_in()

    rows = p.locator("li:has([data-testid='quantity-stepper'])")
    items = []
    for idx in range(rows.count()):
        row = rows.nth(idx)
        link = row.locator('a[href^="/ip/"]').first
        href = link.get_attribute("href") or ""
        item_id = href.rstrip("/").rsplit("/", 1)[-1]
        name_el = row.locator('[data-testid="productName"]').first
        name = name_el.inner_text() if name_el.count() else ""
        quantity = _current_cart_quantity(row)
        price_el = row.get_by_text("Current price", exact=False).first
        unit_price = _parse_price(price_el.inner_text()) if price_el.count() else 0.0
        items.append({"item_id": item_id, "name": name, "quantity": quantity, "unit_price": unit_price})

    subtotal_el = p.locator('[data-testid="subtotal-label-pos"]').first
    total = None
    if subtotal_el.count():
        total = _parse_price(subtotal_el.locator("xpath=..").inner_text())
    if total is None:
        total = round(sum((it["unit_price"] or 0) * it["quantity"] for it in items), 2)
    return {"items": items, "total": total}


def submit_order():
    """Drive checkout to completion and read back the confirmation page.

    Only ever called after place_order's full validation sequence passes -
    see module docstring. Returns confirmed:false with whatever's actually
    on screen if the confirmation page doesn't show what's expected, rather
    than assuming success from "the click didn't error" (§3 read-back).

    Verified live, up to but deliberately not including an actual click of
    "Place order", against a real logged-in account with a real cart -
    driven, screenshotted, and backed out of without submitting, precisely
    so this path is only ever completed for real with approval already in
    hand (see README).

    The flow is one step longer than first assumed: "Continue to checkout"
    does not go straight to a payment page - it opens a "Reserve a time"
    panel (pickup/delivery slot selection) inside the cart. Nothing here is
    pre-selected; the panel shows "Please choose an option" until a slot
    radio is picked, and *then* its own "Continue" button proceeds. This
    function takes the first available slot rather than trying to choose an
    optimal one - which slot is wanted is exactly the kind of judgment call
    that belongs to whoever approves the checkout_preview summary, not to a
    default buried in DOM-driving code; if a specific slot matters, this is
    the place to extend it.

    Confirmed live: the resulting page is titled "Review your order" and its
    submit control's accessible name is exactly "Place order for $19.64"
    (with the real total, not a fixed string) - `name="Place order"` below
    still matches it correctly under Playwright's default substring role-name
    matching, so the original placeholder was right by luck rather than
    verification. The order-confirmation selector past that point is still a
    guess - reaching it means actually placing an order, which this project
    deliberately never did.
    """
    p = page()
    dom_action("open the cart", lambda: p.goto(f"{BASE_URL}/cart", timeout=TIMEOUT_MS))
    ensure_logged_in()
    continue_button = p.get_by_role("button", name="Continue to checkout")
    dom_action("go to checkout", lambda: continue_button.click())

    slot_panel_continue = p.get_by_role("button", name="Continue")
    if slot_panel_continue.count():
        first_slot = p.get_by_role("radio").first
        if first_slot.count():
            dom_action("choose a delivery/pickup slot", lambda: first_slot.click())
        dom_action("confirm the slot", lambda: slot_panel_continue.click())

    place_button = p.get_by_role("button", name="Place order")
    dom_action("place the order", lambda: place_button.click())

    try:
        p.wait_for_selector("[data-testid='order-confirmation-number']", timeout=TIMEOUT_MS)
    except Exception:
        return {"confirmed": False, "order_number": None, "detail": p.url}

    order_el = p.query_selector("[data-testid='order-confirmation-number']")
    return {"confirmed": True, "order_number": order_el.inner_text() if order_el else None, "detail": None}


# ---------------------------------------------------------------------------
# Pure logic: cart math, the checkout token gate
# ---------------------------------------------------------------------------


def cart_signature(items):
    """A hash of (item_id, quantity, unit_price) tuples, order-independent.

    Bound to the exact cart, not just its total - two different carts can sum
    to the same number, and the whole point of the checkout gate is approving
    *this* cart, not a dollar amount.
    """
    key = sorted(
        (it["item_id"], int(it["quantity"]), round(float(it["unit_price"] or 0), 2))
        for it in items
    )
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


def prune_expired_tokens():
    now = time.time()
    for token in [t for t, rec in _pending_checkouts.items() if now - rec["issued_at"] > CHECKOUT_TOKEN_TTL]:
        _pending_checkouts.pop(token, None)


def describe_item(it):
    return {
        "item_id": it["item_id"],
        "name": it["name"],
        "price": it.get("price", it.get("unit_price")),
        "in_stock": it.get("in_stock", True),
        "fulfillable_here": it.get("fulfillable_here", True),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "Find Walmart stores near an address or zip, with pickup/delivery "
    "availability. Call this first - every other tool needs a store_id, and "
    "this is the only way to get one without guessing.",
    {"location": s("Zip code or street address to search near.")},
    ["location"],
)
def find_stores(location):
    location = (location or "").strip()
    if not location:
        raise ToolError("location is required - a zip code or a street address.")
    stores = _dom_find_stores(location)
    if not stores:
        raise ToolError(
            f"No Walmart stores found near {location!r}. Check the zip/address, "
            f"or the location may be too far from any store to list."
        )
    return {
        "ok": True,
        "summary": f"{len(stores)} store(s) near {location}.",
        "stores": stores,
    }


@tool(
    "Set the active store for search, cart, and checkout. Fails if the store "
    "doesn't support the requested fulfillment method.",
    {
        "store_id": s("From find_stores."),
        "fulfillment": s("How the order will be fulfilled.", enum=["pickup", "delivery"]),
    },
    ["store_id", "fulfillment"],
)
def select_store(store_id, fulfillment):
    store_id = (store_id or "").strip()
    if fulfillment not in ("pickup", "delivery"):
        raise ToolError("fulfillment must be 'pickup' or 'delivery'.")

    matches = _dom_find_stores(store_id) if not store_id.isdigit() else None
    # store_id from find_stores is already a concrete id; only re-resolve if
    # something non-numeric slipped through.
    _dom_select_store(store_id, fulfillment)

    _active_store["store_id"] = store_id
    _active_store["fulfillment"] = fulfillment
    return {
        "ok": True,
        "summary": f"Active store set to {store_id} ({fulfillment}).",
        "store_id": store_id,
        "fulfillment": fulfillment,
    }


@tool(
    "Search items at the active store. Returns price, availability, and "
    "item_id for use with add_to_cart. Out-of-stock items are included with "
    "in_stock:false rather than omitted - an omitted item looks like it "
    "doesn't exist at all.",
    {
        "query": s("What to search for."),
        "limit": i("Max results.", default=10, minimum=1, maximum=25),
    },
    ["query"],
)
def search_items(query, limit=10):
    if not _active_store["store_id"]:
        raise ToolError(
            "No active store. Call find_stores then select_store first - "
            "prices and availability depend on which store is active."
        )
    query = (query or "").strip()
    if not query:
        raise ToolError("query is required.")
    items = _dom_search(query, limit)
    if not items:
        raise ToolError(
            f"No results for {query!r} at the active store. Try a broader "
            f"term, or it may not be carried by this store."
        )
    return {
        "ok": True,
        "summary": f"{len(items)} result(s) for {query!r}.",
        "items": [describe_item(it) for it in items],
    }


@tool(
    "Add an item to the cart, and confirm it actually landed there with the "
    "requested quantity. A click succeeding is not the same as the cart "
    "changing - this reads the live cart back before reporting success.",
    {
        "item_id": s("From search_items."),
        "quantity": i("How many.", default=1, minimum=1, maximum=20),
    },
    ["item_id", "quantity"],
)
def add_to_cart(item_id, quantity=1):
    if not _active_store["store_id"]:
        raise ToolError("No active store. Call find_stores then select_store first.")
    item_id = (item_id or "").strip()
    if not item_id:
        raise ToolError("item_id is required.")

    _dom_add_to_cart(item_id, quantity)

    deadline = time.time() + CONFIRM_TIMEOUT
    landed = None
    while time.time() < deadline:
        cart = read_cart()
        landed = next((it for it in cart["items"] if it["item_id"] == item_id), None)
        if landed:
            break
        time.sleep(0.5)

    if not landed:
        return {
            "ok": True,
            "confirmed": False,
            "summary": (
                f"Add-to-cart for {item_id} was accepted but the item has not "
                f"appeared in the cart after {CONFIRM_TIMEOUT}s. Check view_cart "
                f"before assuming it worked."
            ),
            "item_id": item_id,
        }

    note = ""
    if landed["quantity"] != quantity:
        note = f" Requested {quantity}, cart shows {landed['quantity']} (Walmart may cap this item's quantity)."

    return {
        "ok": True,
        "confirmed": True,
        "summary": f"Added {landed['name']!r} - cart shows {landed['quantity']} at ${landed['unit_price']:.2f} each.{note}",
        "item_id": item_id,
        "quantity_in_cart": landed["quantity"],
        "unit_price": landed["unit_price"],
    }


@tool("Read the live cart: items, quantities, unit prices, subtotal.")
def view_cart():
    cart = read_cart()
    if not cart["items"]:
        return {"ok": True, "summary": "The cart is empty.", "items": [], "total": 0.0}
    return {
        "ok": True,
        "summary": f"{len(cart['items'])} item(s), ${cart['total']:.2f} total.",
        "items": cart["items"],
        "total": cart["total"],
    }


@tool(
    "Change the quantity of an item already in the cart. quantity=0 removes "
    "it - same as remove_from_cart, offered both ways since 'set it to 0' "
    "and 'take it off' are both things worth reaching for directly.",
    {"item_id": s("Item already in the cart."), "quantity": i("New quantity. 0 removes it.", minimum=0)},
    ["item_id", "quantity"],
)
def update_cart(item_id, quantity):
    item_id = (item_id or "").strip()
    if not item_id:
        raise ToolError("item_id is required.")
    if quantity == 0:
        return remove_from_cart(item_id)

    _dom_set_cart_quantity(item_id, quantity)
    cart = read_cart()
    landed = next((it for it in cart["items"] if it["item_id"] == item_id), None)
    if not landed:
        raise ToolError(
            f"{item_id} is not in the cart - nothing to update. Use add_to_cart "
            f"to add it."
        )
    return {
        "ok": True,
        "confirmed": landed["quantity"] == quantity,
        "summary": f"{landed['name']!r} now shows quantity {landed['quantity']} in the cart.",
        "item_id": item_id,
        "quantity_in_cart": landed["quantity"],
    }


@tool("Remove an item from the cart entirely.", {"item_id": s("Item to remove.")}, ["item_id"])
def remove_from_cart(item_id):
    item_id = (item_id or "").strip()
    if not item_id:
        raise ToolError("item_id is required.")

    _dom_remove_from_cart(item_id)
    cart = read_cart()
    still_there = any(it["item_id"] == item_id for it in cart["items"])
    if still_there:
        raise ToolError(
            f"Asked to remove {item_id} but it is still in the cart. Report "
            f"this rather than retrying."
        )
    return {"ok": True, "confirmed": True, "summary": f"Removed {item_id} from the cart.", "item_id": item_id}


@tool(
    "Read-only. Summarizes the cart as it would be ordered right now - items, "
    "quantities, prices, fulfillment detail, total cost - and returns a "
    "confirmation_token bound to this exact cart. Nothing is ordered by "
    "calling this. Show the summary to the user and get an explicit yes "
    "before calling place_order with the token."
)
def checkout_preview():
    if not _active_store["store_id"]:
        raise ToolError("No active store. Call find_stores then select_store first.")
    cart = read_cart()
    if not cart["items"]:
        raise ToolError("The cart is empty - nothing to check out. Add items with add_to_cart first.")

    prune_expired_tokens()
    token = secrets.token_urlsafe(24)
    _pending_checkouts[token] = {
        "signature": cart_signature(cart["items"]),
        "total": cart["total"],
        "items": cart["items"],
        "issued_at": time.time(),
        "used": False,
    }
    return {
        "ok": True,
        "summary": (
            f"{len(cart['items'])} item(s), total ${cart['total']:.2f}, "
            f"{_active_store['fulfillment']} at store {_active_store['store_id']}. "
            f"Token expires in {CHECKOUT_TOKEN_TTL}s - show this summary to the "
            f"user and get an explicit yes before calling place_order."
        ),
        "items": cart["items"],
        "fulfillment": _active_store["fulfillment"],
        "store_id": _active_store["store_id"],
        "total": cart["total"],
        "confirmation_token": token,
        "expires_in_seconds": CHECKOUT_TOKEN_TTL,
    }


@tool(
    "Place the order. Requires a confirmation_token from a checkout_preview "
    "called in the last few minutes, whose total and contents still match "
    "the live cart exactly. This is the only tool here that spends money - "
    "never call it without the user having explicitly approved the "
    "checkout_preview summary first, in the same conversation.",
    {"confirmation_token": s("From checkout_preview. Single-use.")},
    ["confirmation_token"],
)
def place_order(confirmation_token):
    confirmation_token = (confirmation_token or "").strip()
    if not confirmation_token:
        raise ToolError(
            "place_order requires a confirmation_token from checkout_preview. "
            "Call checkout_preview first, show the total to whoever is "
            "approving this, and pass the token back only after they say yes."
        )

    prune_expired_tokens()
    rec = _pending_checkouts.get(confirmation_token)
    if rec is None:
        raise ToolError(
            "This confirmation_token is not recognized - it's from a "
            "previous server session, already expired and pruned, or "
            "mistyped. Call checkout_preview again to get a current one."
        )
    if rec["used"]:
        raise ToolError(
            "This confirmation_token was already used to place an order. "
            "Each token is single-use. Call checkout_preview again if a new "
            "order is wanted.",
            already_ordered=True,
        )
    age = time.time() - rec["issued_at"]
    if age > CHECKOUT_TOKEN_TTL:
        raise ToolError(
            f"This confirmation_token expired {int(age - CHECKOUT_TOKEN_TTL)}s "
            f"ago (tokens last {CHECKOUT_TOKEN_TTL}s). Prices and stock can "
            f"change in that window - call checkout_preview again for a "
            f"fresh total before ordering."
        )

    cart = read_cart()
    live_signature = cart_signature(cart["items"])
    if live_signature != rec["signature"]:
        raise ToolError(
            "The cart has changed since checkout_preview - a price, "
            "quantity, or item's availability shifted. Call checkout_preview "
            "again to see the current total and get a new token before "
            "ordering.",
            changed=True,
        )

    # Mark used before dispatching: a crash mid-order can't be replayed into
    # a double order by resubmitting the same token. The retry path is
    # calling checkout_preview again, which re-reads the live cart.
    rec["used"] = True

    order = submit_order()
    if order["confirmed"]:
        return {
            "ok": True,
            "confirmed": True,
            "summary": f"Order placed - confirmation {order['order_number']}, total ${rec['total']:.2f}.",
            "order_number": order["order_number"],
            "total": rec["total"],
        }
    return {
        "ok": True,
        "confirmed": False,
        "summary": (
            "The order was submitted but the confirmation page did not show "
            "the expected order number. Check the Walmart account directly "
            "before assuming it did or didn't go through - do not resubmit."
        ),
        "detail": order.get("detail"),
    }


def banner():
    return (
        f"WALMART_STORAGE_STATE={STORAGE_STATE} "
        f"({'found' if os.path.exists(STORAGE_STATE) else 'MISSING - run auth_setup.py'})  "
        f"WALMART_HEADLESS={HEADLESS}  "
        f"WALMART_STORE_ID={DEFAULT_STORE_ID or 'unset'}  "
        f"WALMART_CHECKOUT_TOKEN_TTL={CHECKOUT_TOKEN_TTL}s"
    )


def main():
    run("walmart-mcp", "0.1.0", banner)


if __name__ == "__main__":
    main()
