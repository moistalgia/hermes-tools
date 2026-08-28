"""walmart-mcp: the checkout gate, and the cart-quantity routing around it.

There's no live Walmart to test against, so the split here is deliberate: the
DOM-touching helpers (search, cart clicks, the checkout handoff window) stay
integration-only, proven by a human running the CLI against the real site
(see README's "Prove it"). Everything below is what doesn't need a browser at
all - and it's also the highest-value surface in the server. A bug in the
DOM selectors fails loudly (a ToolError naming the page). A bug in the
checkout token gate fails silently as a checkout window opened without real
approval, which is exactly the kind of "confident wrong answer" DESIGN.md
says is worth testing.

Every test that reaches checkout_preview or open_checkout swaps out
`read_cart`/`open_checkout_window` for a fake rather than touching Playwright -
read_cart is the same chokepoint qbt-mcp's `torrents/info` read-back plays,
just faked here instead of hit over HTTP.
"""

import time
import unittest

from support import load

walmart = load("walmart_under_test", "walmart-mcp/walmart_mcp_server.py",
                env={"WALMART_STORE_ID": None})


def fake_cart(items, total=None):
    if total is None:
        total = round(sum(it["unit_price"] * it["quantity"] for it in items), 2)
    return {"items": items, "total": total}


ITEM_A = {"item_id": "111", "name": "Whole Milk, Gallon", "quantity": 1, "unit_price": 3.99}
ITEM_B = {"item_id": "222", "name": "Bananas, 1 lb", "quantity": 3, "unit_price": 0.58}


class WalmartCase(unittest.TestCase):
    """Each test gets a clean token table and an active store."""

    def setUp(self):
        walmart._pending_checkouts.clear()
        walmart._active_store["store_id"] = "3142"
        walmart._active_store["fulfillment"] = "pickup"
        walmart.CONFIRM_TIMEOUT = 5
        walmart.CHECKOUT_TOKEN_TTL = 300
        self._orig_read_cart = walmart.read_cart
        self._orig_open_checkout_window = walmart.open_checkout_window
        self._orig_dom_add = walmart._dom_add_to_cart
        self._orig_dom_set_qty = walmart._dom_set_cart_quantity
        self._orig_dom_remove = walmart._dom_remove_from_cart

    def tearDown(self):
        walmart.read_cart = self._orig_read_cart
        walmart.open_checkout_window = self._orig_open_checkout_window
        walmart._dom_add_to_cart = self._orig_dom_add
        walmart._dom_set_cart_quantity = self._orig_dom_set_qty
        walmart._dom_remove_from_cart = self._orig_dom_remove


class TestCartSignature(WalmartCase):
    def test_stable_under_reordering(self):
        sig1 = walmart.cart_signature([ITEM_A, ITEM_B])
        sig2 = walmart.cart_signature([ITEM_B, ITEM_A])
        self.assertEqual(sig1, sig2)

    def test_sensitive_to_price_change(self):
        bumped = dict(ITEM_A, unit_price=4.29)
        self.assertNotEqual(
            walmart.cart_signature([ITEM_A]),
            walmart.cart_signature([bumped]),
        )

    def test_sensitive_to_quantity_change(self):
        bumped = dict(ITEM_A, quantity=2)
        self.assertNotEqual(
            walmart.cart_signature([ITEM_A]),
            walmart.cart_signature([bumped]),
        )

    def test_two_carts_with_the_same_total_are_different_signatures(self):
        # $3.99 x 1 and $1.33 x 3 both total $3.99 - the whole point of
        # signing the cart instead of the total.
        other = {"item_id": "999", "name": "Something else", "quantity": 3, "unit_price": 1.33}
        self.assertNotEqual(
            walmart.cart_signature([ITEM_A]),
            walmart.cart_signature([other]),
        )


class TestCheckoutPreview(WalmartCase):
    def test_empty_cart_refuses_to_mint_a_token(self):
        walmart.read_cart = lambda: fake_cart([])
        with self.assertRaises(walmart.ToolError) as caught:
            walmart.checkout_preview()
        self.assertIn("empty", str(caught.exception))
        self.assertEqual(walmart._pending_checkouts, {})

    def test_no_active_store_refuses(self):
        walmart._active_store["store_id"] = None
        walmart.read_cart = lambda: fake_cart([ITEM_A])
        with self.assertRaises(walmart.ToolError):
            walmart.checkout_preview()

    def test_returns_a_usable_token_bound_to_the_cart(self):
        walmart.read_cart = lambda: fake_cart([ITEM_A, ITEM_B])
        result = walmart.checkout_preview()
        self.assertTrue(result["ok"])
        self.assertIn("confirmation_token", result)
        token = result["confirmation_token"]
        self.assertIn(token, walmart._pending_checkouts)
        rec = walmart._pending_checkouts[token]
        self.assertEqual(rec["signature"], walmart.cart_signature([ITEM_A, ITEM_B]))
        self.assertFalse(rec["used"])


class TestOpenCheckout(WalmartCase):
    """open_checkout never places an order - it opens a handoff window and
    stops. What's being tested is that the gate in front of that window is
    hard to bypass, not anything about the window itself (that part is
    integration-only, see module docstring)."""

    def _preview(self, items):
        walmart.read_cart = lambda: fake_cart(items)
        return walmart.checkout_preview()["confirmation_token"]

    def test_no_token_is_refused(self):
        with self.assertRaises(walmart.ToolError):
            walmart.open_checkout("")

    def test_unknown_token_is_refused(self):
        with self.assertRaises(walmart.ToolError):
            walmart.open_checkout("not-a-real-token")

    def test_reused_token_is_refused(self):
        token = self._preview([ITEM_A])
        walmart.open_checkout_window = lambda: None
        walmart.open_checkout(token)
        with self.assertRaises(walmart.ToolError) as caught:
            walmart.open_checkout(token)
        self.assertTrue(caught.exception.extra.get("already_opened"))

    def test_expired_token_is_refused(self):
        token = self._preview([ITEM_A])
        walmart._pending_checkouts[token]["issued_at"] = time.time() - walmart.CHECKOUT_TOKEN_TTL - 5
        with self.assertRaises(walmart.ToolError) as caught:
            walmart.open_checkout(token)
        self.assertIn("expired", str(caught.exception))

    def test_cart_changed_since_preview_is_refused(self):
        token = self._preview([ITEM_A])
        # Price moved between preview and the handoff.
        walmart.read_cart = lambda: fake_cart([dict(ITEM_A, unit_price=4.29)])
        with self.assertRaises(walmart.ToolError) as caught:
            walmart.open_checkout(token)
        self.assertTrue(caught.exception.extra.get("changed"))

    def test_matching_fresh_token_opens_the_window_once(self):
        token = self._preview([ITEM_A])
        calls = []
        walmart.open_checkout_window = lambda: calls.append(1)
        result = walmart.open_checkout(token)
        self.assertTrue(result["ok"])
        self.assertTrue(result["handoff"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(walmart._pending_checkouts[token]["used"])

    def test_a_window_failure_still_leaves_the_token_spent(self):
        # Mirrors qbt-mcp's "mark used before dispatching" reasoning, just
        # for a window instead of an order: a crash opening the window
        # should not let the same token be replayed into two windows.
        token = self._preview([ITEM_A])

        def boom():
            raise walmart.ToolError("display not available")
        walmart.open_checkout_window = boom

        with self.assertRaises(walmart.ToolError):
            walmart.open_checkout(token)
        self.assertTrue(walmart._pending_checkouts[token]["used"])


class TestUpdateCart(WalmartCase):
    def test_zero_quantity_routes_to_remove(self):
        calls = []
        walmart._dom_remove_from_cart = lambda item_id: calls.append(("remove", item_id))
        walmart._dom_set_cart_quantity = lambda item_id, qty: calls.append(("set", item_id, qty))
        walmart.read_cart = lambda: fake_cart([])
        walmart.update_cart("111", 0)
        self.assertEqual(calls, [("remove", "111")])

    def test_positive_quantity_routes_to_set(self):
        calls = []
        walmart._dom_remove_from_cart = lambda item_id: calls.append(("remove", item_id))
        walmart._dom_set_cart_quantity = lambda item_id, qty: calls.append(("set", item_id, qty))
        walmart.read_cart = lambda: fake_cart([dict(ITEM_A, quantity=5)])
        result = walmart.update_cart("111", 5)
        self.assertEqual(calls, [("set", "111", 5)])
        self.assertTrue(result["confirmed"])

    def test_updating_an_item_not_in_the_cart_is_an_error(self):
        walmart._dom_set_cart_quantity = lambda item_id, qty: None
        walmart.read_cart = lambda: fake_cart([])
        with self.assertRaises(walmart.ToolError):
            walmart.update_cart("999", 2)


class TestAddToCart(WalmartCase):
    def test_confirmed_when_the_item_shows_up(self):
        walmart._dom_add_to_cart = lambda item_id, quantity: None
        walmart.read_cart = lambda: fake_cart([dict(ITEM_A, quantity=2)])
        result = walmart.add_to_cart("111", 2)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["quantity_in_cart"], 2)

    def test_unconfirmed_when_the_item_never_shows_up(self):
        walmart.CONFIRM_TIMEOUT = 0  # don't actually wait in a test
        walmart._dom_add_to_cart = lambda item_id, quantity: None
        walmart.read_cart = lambda: fake_cart([])
        result = walmart.add_to_cart("111", 1)
        self.assertFalse(result["confirmed"])

    def test_quantity_cap_is_reported_not_hidden(self):
        walmart._dom_add_to_cart = lambda item_id, quantity: None
        # Requested 10, Walmart capped it at 6.
        walmart.read_cart = lambda: fake_cart([dict(ITEM_A, quantity=6)])
        result = walmart.add_to_cart("111", 10)
        self.assertTrue(result["confirmed"])
        self.assertIn("Requested 10", result["summary"])


if __name__ == "__main__":
    unittest.main()
