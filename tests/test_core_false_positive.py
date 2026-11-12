"""False-positive tuning tests for Preflight custom detectors."""

from __future__ import annotations

import pytest

from preflight.core.pipeline import run_analysis
from preflight.types import Verdict


class TestCleanFixtures:
    """Clean fixtures must not be flagged."""

    @pytest.mark.asyncio
    async def test_clean_reentrancyguard_vault_looks_ok(
        self, clean_reentrancyguard_vault: str
    ):
        report = await run_analysis(
            clean_reentrancyguard_vault, contract_name="CleanReentrancyGuardVault"
        )
        assert report.verdict == "looks_ok"

    @pytest.mark.asyncio
    async def test_clean_oracle_fresh_looks_ok(self, clean_oracle_fresh: str):
        report = await run_analysis(
            clean_oracle_fresh, contract_name="CleanOracleFresh"
        )
        assert report.verdict == "looks_ok"


class TestVulnerableRegression:
    """Existing vulnerable fixtures must keep their expected verdicts."""

    @pytest.mark.asyncio
    async def test_reentrancy_vulnerable_still_blocked(self, reentrancy_vulnerable: str):
        report = await run_analysis(
            reentrancy_vulnerable, contract_name="ReentrancyVulnerable"
        )
        assert report.verdict == "block"

    @pytest.mark.asyncio
    async def test_unguarded_uups_still_blocked_or_review(self, unguarded_uups: str):
        report = await run_analysis(unguarded_uups, contract_name="UnguardedUUPS")
        assert report.verdict in ("block", "review")

    @pytest.mark.asyncio
    async def test_oracle_no_freshness_still_review(self, oracle_no_freshness: str):
        report = await run_analysis(
            oracle_no_freshness, contract_name="OracleNoFreshness"
        )
        assert report.verdict == "review"

    @pytest.mark.asyncio
    async def test_honeypot_pausable_still_blocked(self, honeypot_pausable: str):
        report = await run_analysis(
            honeypot_pausable, contract_name="HoneypotToken"
        )
        assert report.verdict == "block"
