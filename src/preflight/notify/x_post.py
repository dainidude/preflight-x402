"""Post verdict cards to X (Twitter) from the daily digest.

Uses OAuth 1.0a (the X API's user-context signing) with the four credentials
from the app's "Keys and Tokens" page. No new pip deps — the signing is
hand-rolled stdlib (the same method twauth uses, implemented in ~40 lines).

Free tier allows ~500 posts/month; we post at most 3/day (trending verdicts).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
import uuid
from typing import Any

import httpx

_API_URL = "https://api.twitter.com/2/tweets"


def _oauth1_header(
    method: str,
    url: str,
    params: dict[str, str],
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
) -> str:
    """Build an OAuth 1.0a Authorization header (HMAC-SHA1 signing)."""

    def enc(s: str) -> str:
        return urllib.parse.quote(s, safe="")

    oauth_params: dict[str, str] = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
        **{f"oauth_{k}": v for k, v in params.items()},
    }

    # Signature base: METHOD&url&sorted-encoded-params
    sorted_params = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(oauth_params.items()))
    base_string = f"{method}&{enc(url)}&{enc(sorted_params)}"

    signing_key = f"{enc(api_secret)}&{enc(access_token_secret)}"
    signature = hmac.new(
        signing_key.encode(), base_string.encode(), hashlib.sha1
    ).digest()
    # RFC 2045 base64 without padding
    import base64

    sig_b64 = base64.b64encode(signature).decode().rstrip("=")

    oauth_params["oauth_signature"] = sig_b64

    header = "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth_params.items()))
    return header


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

    # X v2 endpoint takes JSON body, params go in the URL
    url = f"{_API_URL}?text={urllib.parse.quote(text)}"
    auth = _oauth1_header("POST", _API_URL, {"text": urllib.parse.quote(text)},
                          api_key, api_secret, access_token, access_secret)

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(_API_URL, headers={"Authorization": auth},
                                        json={"text": text})
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("data", {}).get("id")
            print(f"[x_post] X API returned {resp.status_code}: {resp.text[:200]}")
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
