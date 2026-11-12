"""Shared type contract for Preflight.

Owned by the arbiter (opencode). All areas import from here — do not duplicate
these models elsewhere. Change requests go through docs/STATUS_*.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]
Verdict = Literal["looks_ok", "review", "block"]

SEVERITY_ORDER: dict[str, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "info": 1,
}


class Finding(BaseModel):
    """One security finding produced by a detector."""

    id: str = Field(description="Stable detector id, e.g. 'reentrancy-no-eth' or 'custom:fee-on-transfer'")
    title: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, description="Detector confidence in the finding")
    description: str
    snippet: str | None = Field(default=None, description="Relevant source snippet, max ~10 lines")
    detector: str = Field(description="Tool that produced it: 'slither' | 'custom:<name>' | 'heuristic:<name>'")
    reference: str | None = Field(default=None, description="Doc/reference URL")


class ScanRequest(BaseModel):
    """Body of POST /v1/scan."""

    source: str = Field(min_length=1, max_length=2_000_000, description="Solidity source (single file)")
    contract_name: str | None = Field(default=None, description="Target contract if source has several")
    solc_version: str | None = Field(default=None, description="e.g. '0.8.20'; auto-detected from pragma when omitted")


class ScanReport(BaseModel):
    """The product: a signed, severity-scored security verdict."""

    verdict: Verdict
    score: float = Field(ge=0.0, le=100.0, description="0 = clean, 100 = certainly dangerous")
    findings: list[Finding] = Field(default_factory=list)
    contract_name: str | None = None
    solc_version: str | None = None
    scanner_versions: dict[str, str] = Field(default_factory=dict, description="e.g. {'slither': '0.10.4', 'custom': '0.1.0'}")
    scanned_at: str = Field(description="ISO-8601 UTC timestamp")
    source_sha256: str = Field(description="SHA-256 of the analyzed source (the cache key)")
    attestation: dict[str, Any] | None = Field(default=None, description="EIP-712 attestation from attestation.sign_report, when configured")
    notes: list[str] = Field(default_factory=list, description="Caveats, e.g. 'simulation tier not included in v1'")


class ProjectScanRequest(BaseModel):
    """Body of POST /v1/scan/project (multi-file)."""

    files: dict[str, str] = Field(min_length=1, max_length=64, description="file name -> Solidity source")
    contract_name: str | None = None
    solc_version: str | None = None


class AddressRiskRequest(BaseModel):
    """Body of POST /v1/address-risk."""

    address: str = Field(min_length=42, max_length=42)


class AddressRiskReport(BaseModel):
    verdict: Verdict
    address: str
    findings: list[Finding] = Field(default_factory=list)
    checked_at: str
    attestation: dict[str, Any] | None = None


class SimRequest(BaseModel):
    """Body of POST /v1/sim and POST /v1/sim/deep — pre-flight simulation.

    `to` is required (the contract being called). `data` is 0x-hex calldata
    (omit for a plain value transfer). `value_wei` is the wei sent. `source`
    optionally carries Solidity source of the target contract so static
    honeypot detectors can run alongside the RPC simulation. `mode` selects
    quick (eth_call) or deep (local anvil fork: gas, balance deltas, traces).
    The /v1/sim/deep endpoint forces mode="deep" server-side.
    """

    to: str = Field(min_length=42, max_length=42)
    data: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]*$")
    value_wei: str | None = Field(default=None, description="Wei value, decimal string")
    from_address: str | None = Field(default=None, description="Optional sender override")
    source: str | None = Field(default=None, max_length=2_000_000)
    mode: str = Field(default="quick", pattern=r"^(quick|deep)$")


class SimReport(BaseModel):
    """Result of a transaction pre-flight simulation."""

    verdict: Verdict
    score: float = Field(ge=0.0, le=100.0)
    findings: list[Finding] = Field(default_factory=list)
    to: str
    from_address: str = Field(description="Address the call was simulated from")
    value_wei: str = Field(default="0")
    reverted: bool = Field(description="Did the simulated call revert?")
    revert_reason: str | None = None
    gas_used: str | None = Field(default=None, description="Gas used, if measured")
    balance_delta_wei: str | None = Field(
        default=None, description="Net wei balance change of the sender (deep mode)"
    )
    internal_calls: list[str] = Field(
        default_factory=list, description="Internal calls observed (deep mode), e.g. ['to → fn(sel4)']"
    )
    simulated_at: str = Field(description="ISO-8601 UTC timestamp")
    simulator: str = Field(description="e.g. 'rpc-ethcall:v1' or 'anvil-fork:v1'")
    attestation: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)
