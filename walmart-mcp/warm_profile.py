#!/usr/bin/env python3
"""
warm_profile - optional, human-run script to give walmart-mcp's persistent
browser profile a head start.

Not an MCP tool. Never decorated with @tool, never registered anywhere the
agent can reach it, and it never runs as part of `serve`.

Why this exists: walmart-mcp runs on a persistent Chrome profile
(WALMART_PROFILE_DIR) specifically because a brand-new, history-less browser
gets challenged by Walmart's bot-check more than one with real visit history
behind it - confirmed by direct testing (see walmart_mcp_server.py's module
docstring, "One browser, headful, persistent, and shared with checkout").
A fresh profile still starts at zero trust, though, and has to earn it
through real use over time. Since this project's checkout handoff already
shares that same profile with a real account login (a deliberate choice -
see README's "One browser, one profile"), logging in here once by hand is a
shortcut to the single strongest trust signal available, rather than waiting
for that to happen organically through a future open_checkout.

This is entirely optional. walmart-mcp works fine starting from an empty
profile - it will just be more likely to hit a "Robot or human?" challenge
in its first uses, same as any brand-new browser install would.

What it does: opens the exact same profile directory, browser, launch args,
and stealth patches walmart_mcp_server.py itself uses (imported from there,
not duplicated, so the two can never drift apart) - headful, and leaves it
open for you to browse and log in by hand. There is no separate save step:
the profile directory itself is the persistence, so whatever's open when you
close the window is what walmart-mcp reuses from then on.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walmart_mcp_server import PROFILE_DIR  # noqa: E402


def main():
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print(
            "playwright and playwright-stealth are required. Run:\n"
            "  pip install playwright playwright-stealth\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(PROFILE_DIR, exist_ok=True)
    print(f"Opening the walmart-mcp profile at: {PROFILE_DIR}")
    print("Browse normally, log in if you'd like to - there's no separate")
    print("save step, this profile is what walmart-mcp will reuse from now on.")

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        Stealth().apply_stealth_sync(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.walmart.com")

        input("\nPress Enter here when you're done browsing... ")

        context.close()

    print(f"\nDone. walmart-mcp will pick up this profile at {PROFILE_DIR} next run.")


if __name__ == "__main__":
    main()
