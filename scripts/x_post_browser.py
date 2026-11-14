#!/usr/bin/env python3
"""Post to X (Twitter) via browser automation — no API needed.

First run: opens a browser window for you to log into X manually.
Subsequent runs: use the saved session (headless) to post.

Usage:
    uv run python scripts/x_post_browser.py login    # one-time: opens browser, log in, saves session
    uv run python scripts/x_post_browser.py post "text"  # headless post
    uv run python scripts/x_post_browser.py verdicts     # post today's verdicts from /verdicts
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

SESSION_FILE = Path.home() / ".preflight-x-session.json"
VERDICTS_URL = "https://preflight--preflight-api-api.us-east.modal.run/verdicts"


def login() -> None:
    """Open a visible browser, let the user log in, save the session."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://x.com/login")

        print("Log into X in the browser window, then press Enter here when done.")
        input()

        # Save cookies + localStorage for the session
        state = context.storage_state()
        SESSION_FILE.write_text(json.dumps(state))
        print(f"Session saved to {SESSION_FILE}")
        browser.close()


def post(text: str) -> bool:
    """Post a tweet using the saved session (headless)."""
    from playwright.sync_api import sync_playwright

    if not SESSION_FILE.exists():
        print("No session — run 'login' first")
        return False

    state = json.loads(SESSION_FILE.read_text())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state)
        page = context.new_page()

        try:
            page.goto("https://x.com/compose/post", wait_until="networkidle", timeout=30000)

            # Wait for the compose box
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10000)
            page.fill('[data-testid="tweetTextarea_0"]', text)
            page.click('[data-testid="tweetButton"]')
            page.wait_for_timeout(2000)  # let the post go through

            # Save refreshed session
            SESSION_FILE.write_text(json.dumps(context.storage_state()))
            print(f"Posted: {text[:60]}…")
            return True
        except Exception as exc:
            print(f"Post failed: {exc}")
            # Save state anyway (might still be logged in)
            try:
                SESSION_FILE.write_text(json.dumps(context.storage_state()))
            except Exception:
                pass
            return False
        finally:
            browser.close()


def post_verdicts() -> None:
    """Fetch today's verdicts from /verdicts and post the interesting ones."""
    try:
        data = json.load(urllib.request.urlopen(VERDICTS_URL, timeout=30))
        verdicts = data.get("verdicts", [])
    except Exception as exc:
        print(f"Could not fetch verdicts: {exc}")
        return

    # Post the most interesting verdict (block > review > looks_ok)
    interesting = [v for v in verdicts if v["verdict"] == "block"]
    if not interesting:
        interesting = [v for v in verdicts if v["verdict"] == "review"]
    if not interesting:
        print("No block/review verdicts to post today")
        return

    v = interesting[0]
    emoji = {"block": "🚨", "review": "⚠️"}[v["verdict"]]
    tweet = (
        f"{emoji} {v['symbol']} on Base: {v['verdict']} — "
        f"{v.get('top_finding') or 'concerning findings'}\n\n"
        "Check any token before you trade: https://preflight--preflight-api-api.us-east.modal.run/v1/token-risk\n"
        "Pay-per-call in USDC via x402. No signup."
    )
    if len(tweet) > 280:
        tweet = tweet[:277] + "…"
    post(tweet)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "login":
        login()
    elif cmd == "post":
        if len(sys.argv) < 3:
            print("Usage: post \"your text\"")
            sys.exit(1)
        post(sys.argv[2])
    elif cmd == "verdicts":
        post_verdicts()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
