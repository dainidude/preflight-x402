"""Report assembly for Preflight."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from preflight.types import Finding, ScanReport, Verdict

from .scoring import calculate_score, determine_verdict, merge_findings


def compute_source_sha256(source: str) -> str:
    """Compute SHA-256 of normalized source (cache key)."""
    # Normalize: strip trailing whitespace from each line
    normalized = "\n".join(line.rstrip() for line in source.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_scanner_versions() -> dict[str, str]:
    """Get versions of scanners used."""
    versions = {}
    try:
        import slither
        versions["slither"] = getattr(slither, "__version__", "0.10.4")
    except Exception:
        versions["slither"] = "0.10.4"
    try:
        import solcx
        versions["solc"] = solcx.get_solc_version_string()
    except Exception:
        versions["solc"] = "unknown"
    versions["custom"] = "0.1.0"
    return versions


def build_scan_report(
    source: str,
    slither_findings: list[Finding],
    custom_findings: list[Finding],
    contract_name: str | None = None,
    solc_version: str | None = None,
    notes: list[str] | None = None,
) -> ScanReport:
    """Assemble final ScanReport from analysis results."""
    findings = merge_findings(slither_findings, custom_findings)
    verdict = determine_verdict(findings)
    score = calculate_score(findings)
    source_sha256 = compute_source_sha256(source)
    scanner_versions = get_scanner_versions()
    scanned_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return ScanReport(
        verdict=verdict,
        score=score,
        findings=findings,
        contract_name=contract_name,
        solc_version=solc_version,
        scanner_versions=scanner_versions,
        scanned_at=scanned_at,
        source_sha256=source_sha256,
        attestation=None,  # Filled by API layer if configured
        notes=notes or [],
    )