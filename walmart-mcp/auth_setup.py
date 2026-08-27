#!/usr/bin/env python3
"""
auth_setup - the human half of walmart-mcp's login.

Not an MCP tool. Never decorated with @tool, never registered anywhere the
agent can reach it, and it never runs as part of `serve`. This is a script a
person runs once, by hand, on the host.

Why login isn't scripted: Walmart's login flow is adversarial toward
automation specifically - bot detection, likely 2FA or a device-verification
prompt on a new fingerprint. Scripting that would mean fighting an arms race
on every tool call. Doing it once, headful, with a human present to clear
whatever challenge shows up, confines that fragility to a setup step that
reruns rarely (whenever Walmart invalidates the session) instead of something
the agent retries mid-conversation - which is the exact failure pattern that
blew out Hermes' context before (a stuck retry loop against Walmart's UI).

What it does: opens a real, visible Chromium window, lets you log in by hand
- including any 2FA - then waits for you to land on a normal walmart.com page
and saves the session (cookies + localStorage) to WALMART_STORAGE_STATE.
Every walmart-mcp tool call after that reuses this file; none of them can
create or refresh it themselves.

Run it again whenever a tool reports the session has expired.

## If the login/verification page spins forever

That's bot-detection, not a slow page - and this script used to be the worst
possible place for it to happen, because it launched a completely vanilla
Playwright browser while the *real* server applied stealth patches (hiding
navigator.webdriver, spoofing plugins/languages) to every session it opened.
A fresh login is exactly when Walmart's bot-checks are most aggressive, so
the one browser in this whole setup that had no patches was the one hitting
them at the worst possible moment. Fixed now - this script imports the same
STEALTH_INIT_SCRIPT and user agent walmart_mcp_server.py uses, so the login
browser looks the same as every later automated session. If a challenge still
spins after this fix, it's a real one (a puzzle, an SMS code) and needs
solving by hand in the window, not a script problem.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walmart_mcp_server import STEALTH_INIT_SCRIPT, USER_AGENT  # noqa: E402

STORAGE_STATE = os.environ.get(
    "WALMART_STORAGE_STATE",
    os.path.join(os.path.expanduser("~"), ".hermes", "walmart_state.json"),
)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(os.path.dirname(STORAGE_STATE) or ".", exist_ok=True)

    print(f"Opening a browser window. Log in to walmart.com, including any 2FA prompt.")
    print(f"Session will be saved to: {STORAGE_STATE}")
    print("Once you're logged in and see a normal walmart.com page, press Enter here.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()
        page.goto("https://www.walmart.com/account/login")

        input("\nPress Enter once logged in... ")

        url = page.url.lower()
        if "login" in url or "account/verify" in url:
            print(
                f"\nStill looks like a login/verification page ({page.url}). "
                f"Saving anyway, but the session may not be valid - re-run this "
                f"script if the first real tool call reports session_expired.",
                file=sys.stderr,
            )

        context.storage_state(path=STORAGE_STATE)
        browser.close()

    print(f"\nSaved. walmart-mcp can now run with WALMART_STORAGE_STATE={STORAGE_STATE}")


if __name__ == "__main__":
    main()
