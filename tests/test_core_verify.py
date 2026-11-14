"""Tests for core/verify.py (E1 attestation verification)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from preflight.attestation import sign_report
from preflight.config import Config
from preflight.core.verify import verify_attestation
from preflight.types import Finding, ScanReport


def _sample_report(source_sha256: str = "a" * 64) -> dict[str, Any]:
    return ScanReport(
        verdict="block",
        score=92.5,
        findings=[
            Finding(
                id="reentrancy-no-eth",
                title="Reentrancy on withdraw()",
                severity="critical",
                confidence=0.98,
                description="External call before state update",
                detector="slither",
            )
        ],
        contract_name="Vault",
        solc_version="0.8.20",
        scanner_versions={"slither": "0.10.4"},
        scanned_at="2026-11-10T00:00:00Z",
        source_sha256=source_sha256,
    ).model_dump(mode="json")


@pytest.fixture
def attest_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREFLIGHT_ATTEST_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("DEV_MODE", "1")
    Config.reload()


def test_verify_valid_attestation_round_trip(attest_env: None) -> None:
    report = _sample_report()
    attestation = sign_report(report)
    assert attestation is not None

    result = verify_attestation(report["source_sha256"], attestation)
    assert result["valid"] is True
    assert result["signer"] == attestation["signer"]
    assert result["verdict"] == report["verdict"]
    assert result["source_sha256"] == report["source_sha256"]
    assert result["checked_at"].endswith("Z")


def test_verify_tampered_digest_fails(attest_env: None) -> None:
    report = _sample_report()
    attestation = sign_report(report)
    assert attestation is not None

    result = verify_attestation("b" * 64, attestation)
    assert result["valid"] is False
    assert "reason" in result


def test_verify_wrong_signer_fails(attest_env: None) -> None:
    report = _sample_report()
    attestation = sign_report(report)
    assert attestation is not None

    attestation["signer"] = "0x" + "22" * 20
    result = verify_attestation(report["source_sha256"], attestation)
    assert result["valid"] is False
    assert result["signer"] == "0x" + "22" * 20


def test_verify_missing_fields_fails(attest_env: None) -> None:
    report = _sample_report()
    attestation = sign_report(report)
    assert attestation is not None

    # Missing signature
    bad = {k: v for k, v in attestation.items() if k != "signature"}
    result = verify_attestation(report["source_sha256"], bad)
    assert result["valid"] is False
    assert "signature" in result["reason"].lower()

    # Malformed non-dict
    result = verify_attestation(report["source_sha256"], "not-a-dict")  # type: ignore[arg-type]
    assert result["valid"] is False
