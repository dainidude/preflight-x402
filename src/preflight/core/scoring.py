"""Scoring and verdict logic for Preflight."""

from __future__ import annotations

from preflight.types import Finding, ScanReport, Severity, Verdict


SEVERITY_ORDER: dict[Severity, int] = {
    "critical": 5,
    "high": 4,
    "medium": 3,
    "low": 2,
    "info": 1,
}

SEVERITY_WEIGHTS: dict[Severity, float] = {
    "critical": 1.0,
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
    "info": 0.1,
}


def determine_verdict(findings: list[Finding]) -> Verdict:
    """Determine verdict based on findings severity.

    Rules (from AGENTS.md):
    - Any critical/high → "block"
    - Any medium → "review"
    - Else → "looks_ok"
    """
    if any(f.severity in ("critical", "high") for f in findings):
        return "block"
    if any(f.severity == "medium" for f in findings):
        return "review"
    return "looks_ok"


def calculate_score(findings: list[Finding]) -> float:
    """Calculate security score 0-100 (higher = worse).

    Weighted by severity and confidence.
    """
    if not findings:
        return 0.0

    total_weight = 0.0
    for finding in findings:
        weight = SEVERITY_WEIGHTS.get(finding.severity, 0.1) * finding.confidence
        total_weight += weight

    # Normalize: max possible weight per finding is 1.0 (critical with confidence 1.0)
    # Scale to 0-100
    max_possible = len(findings) * 1.0
    score = (total_weight / max_possible) * 100.0
    return min(score, 100.0)


def merge_findings(
    slither_findings: list[Finding],
    custom_findings: list[Finding],
) -> list[Finding]:
    """Merge Slither and custom findings, deduplicating by id."""
    seen: dict[str, Finding] = {}
    for f in slither_findings + custom_findings:
        if f.id not in seen:
            seen[f.id] = f
        else:
            # Keep the one with higher confidence
            if f.confidence > seen[f.id].confidence:
                seen[f.id] = f
    return list(seen.values())