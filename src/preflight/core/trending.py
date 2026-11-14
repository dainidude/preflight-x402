"""Trending-token verdicts for the daily digest.

E2: fetch top Base tokens from a free public source and run ``run_token_risk``
on each so the daily digest can surface live honeypot/tax warnings.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from preflight.config import config
from preflight.core.token_risk import run_token_risk
from preflight.types import ScanReport

_GECKOTERMINAL_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/base/pools"
_GECKOTERMINAL_TOKEN_URL = "https://api.geckoterminal.com/api/v2/networks/base/tokens/{address}"


def _token_symbol(address: str) -> str | None:
    """Best-effort symbol lookup on GeckoTerminal."""
    try:
        resp = httpx.get(
            _GECKOTERMINAL_TOKEN_URL.format(address=address),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["data"]["attributes"]["symbol"]
    except Exception:
        return None


def _fallback_tokens(n: int) -> list[dict[str, str]]:
    """When the public source is unreachable, fall back to configured assets."""
    cfg = config()
    candidates: list[str] = []
    usdc = getattr(cfg, "usdc_asset", None)
    if usdc:
        candidates.append(usdc)
    # WETH on Base is a useful, well-known fallback.
    if getattr(cfg, "network", "") == "base":
        candidates.append("0x4200000000000000000000000000000000000006")
    elif getattr(cfg, "network", "") == "base-sepolia":
        candidates.append("0x4200000000000000000000000000000000000006")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for addr in candidates:
        low = addr.lower()
        if low not in seen:
            seen.add(low)
            out.append({"address": addr, "symbol": _token_symbol(addr) or "UNKNOWN"})
        if len(out) >= n:
            break
    return out


def get_trending_base_tokens(n: int = 3) -> list[dict[str, str]]:
    """Return the top ``n`` Base token contract addresses by 24h volume.

    Uses GeckoTerminal's free pools API as the public source, then looks up
    each token's symbol. Falls back to configured assets if the network call
    fails.
    """
    try:
        resp = httpx.get(
            _GECKOTERMINAL_POOLS_URL,
            params={"page": 1},
            timeout=15.0,
        )
        resp.raise_for_status()
        pools = resp.json().get("data", [])
    except Exception:
        return _fallback_tokens(n)

    tokens: list[dict[str, str]] = []
    seen: set[str] = set()

    for pool in pools:
        relationships = pool.get("relationships", {})
        for side in ("base_token", "quote_token"):
            tok = (
                relationships.get(side, {})
                .get("data", {})
                .get("id", "")
            )
            if not tok.startswith("base_"):
                continue
            addr = tok.split("_", 1)[1]  # 'base_0x...' -> '0x...'
            low = addr.lower()
            if low in seen:
                continue
            seen.add(low)
            symbol = _token_symbol(addr) or "UNKNOWN"
            tokens.append({"address": addr, "symbol": symbol})
            if len(tokens) >= n:
                return tokens

    return tokens


def format_trending_cards(reports: list[ScanReport]) -> str:
    """Format ScanReports as one-line Discord verdict cards."""
    if not reports:
        return ""
    lines = ["📈 Trending Base tokens:"]
    emoji = {
        "looks_ok": "✅",
        "review": "⚠️",
        "block": "🚨",
    }
    for report in reports:
        symbol = "UNKNOWN"
        for note in report.notes:
            if note.startswith("Token:"):
                # Token: SYMBOL (0x...)
                m = re.search(r"Token:\s+(\S+)", note)
                if m:
                    symbol = m.group(1)
                break
        top = report.findings[0].title if report.findings else "no findings"
        lines.append(f"{emoji.get(report.verdict, '❓')} {symbol}: {report.verdict} — {top}")
    return "\n".join(lines)


async def scan_trending_tokens(n: int = 3) -> list[ScanReport]:
    """Fetch trending tokens and run the token-risk scanner on each.

    The token address (and symbol) are prepended to each report's notes so
    downstream formatters can attribute the verdict.
    """
    tokens = get_trending_base_tokens(n)
    reports: list[ScanReport] = []
    for tok in tokens:
        addr = tok["address"]
        symbol = tok.get("symbol") or "UNKNOWN"
        try:
            report = await run_token_risk(addr)
        except Exception:
            # A single token failing (e.g., RPC hiccup) should not break the
            # whole digest.
            continue
        report.notes.insert(0, f"Token: {symbol} ({addr})")
        reports.append(report)
    return reports
