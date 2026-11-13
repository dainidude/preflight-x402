"""Edge-case regression tests for custom detectors."""

from __future__ import annotations

import pytest

from preflight.core.pipeline import run_analysis
from preflight.types import Verdict


class TestInterfaceAndAbstractSources:
    """Detectors must not false-positive on interface/abstract declarations."""

    
    def test_interface_only_no_medium_findings(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IOracle {
    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80);
}

interface IToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function pause() external;
}
"""
        report = run_analysis(source)
        medium_or_high = [f for f in report.findings if f.severity in ("medium", "high")]
        assert len(medium_or_high) == 0
        assert report.verdict == "looks_ok"

    
    def test_abstract_virtual_function_no_false_positive(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

abstract contract BaseOracle {
    function latestRoundData() external view virtual returns (uint80, int256, uint256, uint256, uint80);
    function initialize(address _owner) external virtual;
}
"""
        report = run_analysis(source)
        custom_findings = [f for f in report.findings if f.detector.startswith("custom:")]
        # No custom medium/high findings from declarations.
        assert all(f.severity in ("low", "info") for f in custom_findings)


class TestEmptyAndMinimalSources:
    """Minimal sources should not crash or false-positive."""

    
    def test_empty_contract_looks_ok(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Empty {}
"""
        report = run_analysis(source, contract_name="Empty")
        assert report.verdict == "looks_ok"

    
    def test_multiple_pragmas_picks_first(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity >=0.8.0;
pragma solidity <0.9.0;

contract MultiPragma {
    uint256 public value;
}
"""
        report = run_analysis(source, contract_name="MultiPragma")
        assert report.solc_version is not None
        assert report.verdict == "looks_ok"


class TestSolcVersionRobustness:
    """Unbounded / edge pragmas should resolve to a sane pinned version."""

    
    def test_unbounded_gte_pragma(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity >=0.8.0;

contract Unbounded {
    uint256 public x;
}
"""
        report = run_analysis(source, contract_name="Unbounded")
        assert report.solc_version is not None
        assert report.verdict == "looks_ok"

    
    def test_caret_pragma(self):
        source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Caret {
    uint256 public x;
}
"""
        report = run_analysis(source, contract_name="Caret")
        assert report.solc_version in ("0.8.20", "0.8.24", "0.8.26")
