#!/usr/bin/env python3
"""Post to X (Twitter) via browser automation — no API needed.

First run: opens a browser window for you to log into X manually.
Subsequent runs: use the saved profile (headless) to post.

X's login dead-ends (Continue button does nothing after the username step)
inside Playwright's bundled Chromium — the automation fingerprint trips the
anti-bot wall. This script therefore launches the SYSTEM chromium
(channel="chromium") with the AutomationControlled flag masked and a
PERSISTENT profile, so the login behaves like a normal user and the
device-trust/2FA state survives between runs.

Usage:
    uv run python scripts/x_post_browser.py login    # one-time: opens browser, log in, saves profile
    uv run python scripts/x_post_browser.py post "text"  # headless post
    uv run python scripts/x_post_browser.py verdicts     # post today's verdicts from /verdicts
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

SESSION_FILE = Path.home() / ".preflight-x-session.json"
PROFILE_DIR = Path.home() / ".preflight-x-profile"
VERDICTS_URL = "https://preflight--preflight-api-api.us-east.modal.run/verdicts"

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch_context(p: object, headless: bool):  # noqa: ANN202 - playwright type
    """Persistent context on the system chromium; fall back to bundled.

    channel="chromium" uses /usr/bin/chromium — a real browser install
    without Playwright's automation build markers. If the system binary is
    missing, the bundled Chromium is still better than nothing.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chromium",
            headless=headless,
            args=_LAUNCH_ARGS,
            locale="en-US",
        )
    except PlaywrightError:
        return p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
            args=_LAUNCH_ARGS,
            locale="en-US",
        )


def _has_auth_token(state: dict) -> bool:
    """True if the storage state carries X's auth cookie (login completed)."""
    return any(c.get("name") == "auth_token" for c in state.get("cookies", []))


def login() -> None:
    """Open a visible browser, let the user log in, save the profile."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com/login")

        print("Log into X in the browser window — email, username, password,")
        print("2FA, captcha: complete WHATEVER it asks (the username step is")
        print("normal). Press Enter here only once your timeline is visible.")
        input()

        state = context.storage_state()
        if not _has_auth_token(state):
            print(
                "WARNING: no auth_token cookie — the login did not complete.\n"
                "Session NOT saved. Close the window and run 'login' again."
            )
        else:
            SESSION_FILE.write_text(json.dumps(state))
            print(f"Session saved to {SESSION_FILE}")
            print(f"Persistent profile (2FA/device trust): {PROFILE_DIR}")
        context.close()


def post(text: str) -> bool:
    """Post a tweet using the saved profile/session (headless)."""
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists() and not SESSION_FILE.exists():
        print("No session — run 'login' first")
        return False

    with sync_playwright() as p:
        # Prefer the persistent profile (real login state + device trust);
        # fall back to the JSON storage state if only that exists.
        if PROFILE_DIR.exists():
            context = _launch_context(p, headless=True)
        else:
            state = json.loads(SESSION_FILE.read_text())
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state)

        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto("https://x.com/compose/post", wait_until="networkidle", timeout=30000)

            # Wait for the compose box
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10000)
            page.fill('[data-testid="tweetTextarea_0"]', text)
            page.click('[data-testid="tweetButton"]')

            # Wait for confirmation: the success toast (or the compose box
            # closing) — not a blind sleep.
            page.wait_for_selector('[data-testid="toast"]', timeout=10000)

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
            context.close()


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
    link = "https://preflight--preflight-api-api.us-east.modal.run/v1/token-risk"
    base = f"{emoji} {v['symbol']} on Base: {v['verdict']} — {v.get('top_finding') or 'concerning findings'}"
    tail = f"\n\nCheck any token before you trade: {link}\nPay-per-call in USDC via x402. No signup."
    # Never truncate the URL: shrink the finding text instead.
    if len(base) + len(tail) > 280:
        base = base[: 280 - len(tail) - 1] + "…"
        tweet = base + tail
    else:
        tweet = base + tail
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
