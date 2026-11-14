"""Post verdict cards to X (Twitter) from the daily digest.

Uses tweepy (OAuth 1.0a handled correctly by the standard X library).
Free tier allows ~500 posts/month; we post at most 3/day (trending verdicts).
"""

from __future__ import annotations

import os


async def post_tweet(text: str, max_retries: int = 2) -> str | None:
    """Post a single tweet. Returns the tweet ID on success, None on failure.

    Env: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.
    Never raises — a failed X post must not break the daily digest.
    """
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        print("[x_post] X credentials not configured — skipping")
        return None

    import time

    import tweepy

    for attempt in range(max_retries + 1):
        try:
            client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
            )
            resp = client.create_tweet(text=text)
            if resp.data:
                return str(resp.data.get("id", ""))
            print(f"[x_post] X API returned: {resp}")
            return None
        except Exception as exc:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            else:
                print(f"[x_post] X post failed after {max_retries + 1} attempts: {exc}")
    return None


def format_verdict_tweet(symbol: str, verdict: str, top_finding: str, address: str) -> str:
    """Format a single verdict as an X post (≤280 chars)."""
    emoji = {"looks_ok": "✅", "review": "⚠️", "block": "🚨"}
    e = emoji.get(verdict, "❓")
    short_addr = f"{address[:6]}…{address[-4:]}"

    # Template: keep it under 280 chars including the link
    base = f"{e} {symbol} on Base: {verdict} — {top_finding}"
    link = f"\n\nVerified by Preflight (pay-per-call security, USDC on Base)\nCheck any token: https://preflight--preflight-api-api.us-east.modal.run"
    if len(base) > 280 - len(link):
        base = base[: 280 - len(link) - 3] + "…"
    return base + link
