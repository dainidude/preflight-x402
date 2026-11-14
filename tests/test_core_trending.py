"""Tests for core/trending.py (E2 trending-token verdicts)."""

from __future__ import annotations

import pytest

from preflight.core import trending
from preflight.core.trending import format_trending_cards, scan_trending_tokens
from preflight.types import Finding, ScanReport


def _make_report(verdict: str, symbol_note: str, top_finding: str) -> ScanReport:
    return ScanReport(
        verdict=verdict,
        score=50.0,
        findings=[
            Finding(
                id="token:transfer-tax",
                title=top_finding,
                severity="medium",
                confidence=0.9,
                description="test",
                detector="heuristic:token",
            )
        ],
        contract_name=None,
        solc_version=None,
        scanner_versions={"token-risk": "0.1.0"},
        scanned_at="2026-11-10T00:00:00Z",
        source_sha256="0x" + "aa" * 32,
        notes=[symbol_note],
    )


@pytest.mark.asyncio
async def test_scan_trending_tokens_format(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get_trending(n: int = 3) -> list[dict[str, str]]:
        return [
            {"address": "0x" + "11" * 20, "symbol": "MOON"},
            {"address": "0x" + "22" * 20, "symbol": "RUG"},
        ]

    async def fake_run_token_risk(address: str) -> ScanReport:
        calls.append(address)
        if address == "0x" + "11" * 20:
            return _make_report("looks_ok", f"Token: MOON ({address})", "no tax")
        return _make_report("block", f"Token: RUG ({address})", "honeypot transfer restricted")

    monkeypatch.setattr(trending, "get_trending_base_tokens", fake_get_trending)
    monkeypatch.setattr(trending, "run_token_risk", fake_run_token_risk)

    reports = await scan_trending_tokens(3)
    assert len(reports) == 2
    assert calls == ["0x" + "11" * 20, "0x" + "22" * 20]
    assert any("MOON" in note for note in reports[0].notes)
    assert reports[1].verdict == "block"


def test_format_trending_cards() -> None:
    reports = [
        _make_report("looks_ok", "Token: SAFE (0x1111…1111)", "no tax"),
        _make_report("review", "Token: SUS (0x2222…2222)", "owner can pause"),
        _make_report("block", "Token: RUG (0x3333…3333)", "honeypot transfer restricted"),
    ]
    text = format_trending_cards(reports)
    assert text.startswith("📈 Trending Base tokens:")
    assert "✅ SAFE: looks_ok" in text
    assert "⚠️ SUS: review" in text
    assert "🚨 RUG: block" in text
    assert "honeypot transfer restricted" in text


def test_format_trending_cards_empty() -> None:
    assert format_trending_cards([]) == ""


@pytest.mark.network
@pytest.mark.asyncio
async def test_get_trending_base_tokens_real_source() -> None:
    tokens = trending.get_trending_base_tokens(3)
    assert len(tokens) <= 3
    for tok in tokens:
        addr = tok["address"]
        assert addr.startswith("0x")
        assert len(addr) == 42
        assert tok.get("symbol")
