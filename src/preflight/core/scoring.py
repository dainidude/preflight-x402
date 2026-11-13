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

# Max-severity base score used by calculate_score.
BASE_SCORES: dict[Severity, float] = {
    "critical": 95.0,
    "high": 80.0,
    "medium": 50.0,
    "low": 20.0,
    "info": 5.0,
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

    Score starts from a max-severity base and adds diminishing contributions
    from additional findings so that many low-severity findings cannot drown
    out a critical.  Total score is capped below 100.
    """
    if not findings:
        return 0.0

    # Sort by severity (highest first), then confidence.
    sorted_findings = sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 0), f.confidence),
        reverse=True,
    )

    base = BASE_SCORES.get(sorted_findings[0].severity, 5.0)

    bonus = 0.0
    # Additional findings contribute with rank k = 1, 2, ... denominator = k + 1.
    for rank, finding in enumerate(sorted_findings[1:], start=1):
        weight = SEVERITY_WEIGHTS.get(finding.severity, 0.1) * finding.confidence
        bonus += weight / (rank + 1)

    return round(min(base + bonus, 99.0), 2)


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