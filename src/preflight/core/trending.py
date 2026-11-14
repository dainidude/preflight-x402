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

    Uses GeckoTerminal's free pools API. Symbols come from the pool name
    (e.g. "WETH / USDC" → base=WETH, quote=USDC) — no separate lookup needed,
    so no rate-limit issues. Falls back to configured assets if the network
    call fails.
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
        # Pool name format: "TOKEN1 / TOKEN2 0.05%" — split and clean
        pool_name = pool.get("attributes", {}).get("name", "")
        name_parts = pool_name.split(" / ")
        base_symbol = name_parts[0].strip() if name_parts else "UNKNOWN"
        # Quote symbol may have a fee suffix (e.g. "USDC 0.05%") — strip it
        quote_symbol = name_parts[1].split()[0].strip() if len(name_parts) > 1 else "UNKNOWN"

        relationships = pool.get("relationships", {})
        for side, symbol in (("base_token", base_symbol), ("quote_token", quote_symbol)):
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
            tokens.append({"address": addr, "symbol": symbol})
            if len(tokens) >= n:
                return tokens

    return tokens


def format_trending_cards(results: list[dict]) -> str:
    """Format scan results as one-line Discord verdict cards."""
    if not results:
        return ""
    lines = ["📈 Trending Base tokens:"]
    emoji = {
        "looks_ok": "✅",
        "review": "⚠️",
        "block": "🚨",
    }
    for r in results:
        symbol = r["symbol"]
        report = r["report"]
        if report is None:
            lines.append(f"❓ {symbol}: scan_failed — could not analyze")
            continue
        top = report.findings[0].title if report.findings else "no findings"
        lines.append(f"{emoji.get(report.verdict, '❓')} {symbol}: {report.verdict} — {top}")
    return "\n".join(lines)


async def scan_trending_tokens(n: int = 3) -> list[dict]:
    """Fetch trending tokens and run the token-risk scanner on each.

    Returns a list of dicts: {"symbol", "address", "report"} — the report is a
    ScanReport, or None if the detailed scan failed (in which case the token
    still appears in the feed with a "scan_failed" note).
    """
    tokens = get_trending_base_tokens(n)
    results: list[dict] = []
    for tok in tokens:
        addr = tok["address"]
        symbol = tok.get("symbol") or "UNKNOWN"
        report: ScanReport | None = None
        try:
            report = await run_token_risk(addr)
        except Exception as exc:
            print(f"trending: token-risk scan failed for {symbol}: {exc}")
        results.append({"symbol": symbol, "address": addr, "report": report})
    return results
