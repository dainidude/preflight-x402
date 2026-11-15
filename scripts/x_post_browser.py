#!/usr/bin/env python3
"""Post to X (Twitter) via a real browser profile — no API needed.

First run: 'login' opens a COMPLETELY NORMAL browser (a plain process,
zero automation — X's anti-bot wall cannot detect it) for you to log in
by hand. The session lands in a persistent profile.
Subsequent runs: 'post' drives that profile headlessly.

Why not automate the login? X's login dead-ends (the username step's
Continue button does nothing, no password field appears) in ANY
CDP-driven browser — Playwright's bundled Chromium, AND the system
chromium even with the AutomationControlled flag masked. X fingerprints
the automation runtime itself, not just the flags. Only a plain,
non-CDP browser process gets through reliably, so the login step uses
one; automation is limited to authenticated posting afterwards.

Usage:
    uv run python scripts/x_post_browser.py login        # one-time: normal browser, log in by hand
    uv run python scripts/x_post_browser.py post "text"   # headless post
    uv run python scripts/x_post_browser.py verdicts      # post today's verdicts from /verdicts
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SESSION_FILE = Path.home() / ".preflight-x-session.json"
PROFILE_DIR = Path.home() / ".preflight-x-profile"
VERDICTS_URL = "https://preflight--preflight-api-api.us-east.modal.run/verdicts"

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Headless chromium reports a "HeadlessChrome" UA token that X flags;
# posting with a real-Chrome UA and an established session is fine.
_POST_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _chromium_binary() -> str | None:
    """Locate a real browser binary for the plain (non-automated) login."""
    for name in ("chromium", "google-chrome-stable", "google-chrome", "brave-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _profile_has_auth_token(cookies_db: Path) -> bool:
    """True if the profile's cookie DB holds X's auth_token (login done).

    Cookie NAMES are plaintext in Chromium's SQLite (values are encrypted),
    so a read-only name check verifies the login without any browser launch.
    The DB is locked while the browser runs — retry briefly after it closes.
    """
    if not cookies_db.exists():
        return False
    for _ in range(10):
        try:
            con = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT 1 FROM cookies WHERE name = 'auth_token' LIMIT 1"
                ).fetchone()
                return row is not None
            finally:
                con.close()
        except sqlite3.Error:
            time.sleep(1)
    return False


def _launch_context(p: object, headless: bool):  # noqa: ANN202 - playwright type
    """Persistent context on the system chromium; fall back to bundled.

    channel="chromium" uses /usr/bin/chromium — a real browser install
    without Playwright's automation build markers. If the system binary is
    missing, the bundled Chromium is still better than nothing.
    """
    from playwright.sync_api import Error as PlaywrightError

    kwargs: dict = {
        "headless": headless,
        "args": _LAUNCH_ARGS,
        "locale": "en-US",
    }
    if headless:
        kwargs["user_agent"] = _POST_UA
    try:
        return p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chromium", **kwargs
        )
    except PlaywrightError:
        return p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)


def login() -> None:
    """Open a NORMAL browser (plain subprocess, zero automation), let the
    user log in by hand, then verify the session landed in the profile."""
    binary = _chromium_binary()
    if binary is None:
        print("No chromium/chrome binary found — install one first (e.g. pacman -S chromium).")
        sys.exit(1)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            binary,
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://x.com/login",
        ]
    )
    print("A completely normal browser window opened — no automation attached.")
    print("Log into X by hand: email, username, password, 2FA, captcha —")
    print("whatever it asks. This time the username step will proceed.")
    print()
    print("When your timeline is visible, CLOSE the browser window, then")
    print("press Enter here.")
    input()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if _profile_has_auth_token(PROFILE_DIR / "Default" / "Cookies"):
        print("Login verified (auth_token present in the profile).")
        print(f"Persistent profile: {PROFILE_DIR}")
        print("Next: uv run python scripts/x_post_browser.py verdicts")
    else:
        print(
            "WARNING: no auth_token cookie in the profile — the login did not\n"
            "complete. Make sure every window of that profile is closed, then\n"
            "run 'login' again."
        )
        sys.exit(1)


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
