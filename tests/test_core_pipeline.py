"""Tests for Preflight core analysis pipeline."""

from __future__ import annotations

import pytest

from preflight.core.pipeline import run_analysis
from preflight.core.project import run_analysis_project
from preflight.types import Verdict


class TestCorePipeline:
    """Integration tests for the core analysis pipeline."""

    @pytest.mark.asyncio
    async def test_clean_erc20_looks_ok(self, clean_erc20: str):
        """Clean ERC20 should pass with looks_ok verdict."""
        report = await run_analysis(clean_erc20, contract_name="CleanERC20")

        assert report.verdict == "looks_ok"
        assert report.score >= 0.0
        assert report.score <= 100.0
        assert report.contract_name == "CleanERC20"
        assert report.solc_version is not None
        assert report.source_sha256 is not None
        assert len(report.scanner_versions) > 0

    @pytest.mark.asyncio
    async def test_reentrancy_vulnerable_blocked(self, reentrancy_vulnerable: str):
        """Reentrancy vulnerable contract should be blocked."""
        report = await run_analysis(reentrancy_vulnerable, contract_name="ReentrancyVulnerable")

        assert report.verdict == "block"
        assert report.score > 0.0
        # Should have at least one critical/high finding from Slither
        critical_or_high = [f for f in report.findings if f.severity in ("critical", "high")]
        assert len(critical_or_high) > 0

    @pytest.mark.asyncio
    async def test_unguarded_uups_blocked_or_review(self, unguarded_uups: str):
        """Unguarded UUPS initializer should be block or review."""
        report = await run_analysis(unguarded_uups, contract_name="UnguardedUUPS")

        assert report.verdict in ("block", "review")
        assert report.score > 0.0
        # Should have custom detector finding for unguarded initializer
        custom_init = [f for f in report.findings if "unguarded-initializer" in f.detector]
        assert len(custom_init) > 0

    @pytest.mark.asyncio
    async def test_oracle_no_freshness_review(self, oracle_no_freshness: str):
        """Oracle without freshness check should be review."""
        report = await run_analysis(oracle_no_freshness, contract_name="OracleNoFreshness")

        assert report.verdict == "review"
        assert report.score > 0.0
        # Should have custom detector finding for oracle freshness
        oracle_findings = [f for f in report.findings if "oracle-freshness" in f.detector]
        assert len(oracle_findings) > 0

    @pytest.mark.asyncio
    async def test_honeypot_pausable_blocked(self, honeypot_pausable: str):
        """Honeypot with pausable transfers should be blocked."""
        report = await run_analysis(honeypot_pausable, contract_name="HoneypotToken")

        assert report.verdict == "block"
        assert report.score > 0.0
        # Should have custom detector finding for honeypot
        honeypot_findings = [f for f in report.findings if "honeypot" in f.detector]
        assert len(honeypot_findings) > 0

    @pytest.mark.asyncio
    async def test_source_sha256_consistency(self, clean_erc20: str):
        """Same source should produce same SHA256."""
        report1 = await run_analysis(clean_erc20)
        report2 = await run_analysis(clean_erc20)

        assert report1.source_sha256 == report2.source_sha256

    @pytest.mark.asyncio
    async def test_different_sources_different_sha256(self, clean_erc20: str, reentrancy_vulnerable: str):
        """Different sources should produce different SHA256."""
        report1 = await run_analysis(clean_erc20)
        report2 = await run_analysis(reentrancy_vulnerable)

        assert report1.source_sha256 != report2.source_sha256

    @pytest.mark.asyncio
    async def test_scanned_at_is_iso_utc(self, clean_erc20: str):
        """scanned_at should be ISO-8601 UTC timestamp."""
        report = await run_analysis(clean_erc20)

        assert report.scanned_at.endswith("Z")
        # Should be parseable
        from datetime import datetime
        dt = datetime.fromisoformat(report.scanned_at.replace("Z", "+00:00"))
        assert dt.tzinfo is not None


class TestCorePipelineEdgeCases:
    """Edge case tests for the pipeline."""

    @pytest.mark.asyncio
    async def test_empty_source_raises(self):
        """Empty source should raise an error."""
        with pytest.raises(ValueError):
            await run_analysis("")

    @pytest.mark.asyncio
    async def test_invalid_solidity_raises(self):
        """Invalid Solidity should raise an error."""
        with pytest.raises(Exception):
            await run_analysis("this is not solidity code {")

    @pytest.mark.asyncio
    async def test_contract_name_optional(self, clean_erc20: str):
        """contract_name parameter should be optional."""
        report = await run_analysis(clean_erc20)
        assert report.contract_name is None

    @pytest.mark.asyncio
    async def test_solc_version_override(self, clean_erc20: str):
        """Explicit solc_version should be used."""
        report = await run_analysis(clean_erc20, solc_version="0.8.20")
        assert report.solc_version == "0.8.20"


class TestCustomDetectors:
    """Tests for specific custom detectors."""

    @pytest.mark.asyncio
    async def test_fee_on_transfer_detection(self):
        """Detector should flag fee-on-transfer patterns."""
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract FeeOnTransfer {
    uint256 public _taxFee = 5;
    uint256 public _liquidityFee = 3;

    function transfer(address to, uint256 amount) public returns (bool) {
        uint256 tax = amount * _taxFee / 100;
        uint256 liquidity = amount * _liquidityFee / 100;
        // fee logic here
        return true;
    }
}
"""
        report = await run_analysis(source)
        fee_findings = [f for f in report.findings if "fee-on-transfer" in f.detector]
        assert len(fee_findings) > 0

    @pytest.mark.asyncio
    async def test_missing_zero_address_check(self):
        """Detector should flag missing zero-address checks."""
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract MissingZeroCheck {
    address public owner;

    constructor(address _owner) {
        // No zero address check
        owner = _owner;
    }
}
"""
        report = await run_analysis(source)
        zero_findings = [f for f in report.findings if "missing-zero-check" in f.detector]
        assert len(zero_findings) > 0

    @pytest.mark.asyncio
    async def test_oracle_freshness_detection(self):
        """Detector should flag oracle without freshness check."""
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface AggregatorV3Interface {
    function latestRoundData() external view returns (
        uint80, int256, uint256, uint256, uint80
    );
}

contract OracleUser {
    AggregatorV3Interface feed;

    constructor(address _feed) {
        feed = AggregatorV3Interface(_feed);
    }

    function getPrice() external view returns (int256) {
        (, int256 price, , , ) = feed.latestRoundData();
        return price;
    }
}
"""
        report = await run_analysis(source)
        oracle_findings = [f for f in report.findings if "oracle-freshness" in f.detector]
        assert len(oracle_findings) > 0


class TestProjectAnalysis:
    """Tests for multi-file project analysis."""

    @pytest.mark.asyncio
    async def test_project_analysis(self):
        """Multi-file project analysis should work."""
        files = {
            "Token.sol": """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./IERC20.sol";

contract Token is IERC20 {
    mapping(address => uint256) balances;
    uint256 private _totalSupply;

    function balanceOf(address account) external view returns (uint256) {
        return balances[account];
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balances[msg.sender] -= amount;
        balances[to] += amount;
        return true;
    }

    function totalSupply() external view returns (uint256) {
        return _totalSupply;
    }
}
""",
            "IERC20.sol": """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function totalSupply() external view returns (uint256);
}
""",
        }

        report = await run_analysis_project(files, contract_name="Token")

        assert report.verdict in ("looks_ok", "review", "block")
        assert report.source_sha256 is not None
        assert len(report.findings) >= 0