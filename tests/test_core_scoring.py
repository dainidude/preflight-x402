"""Regression tests for Preflight scoring logic."""

from __future__ import annotations

import pytest

from preflight.core.scoring import calculate_score, determine_verdict
from preflight.types import Finding


def _finding(severity: str, confidence: float) -> Finding:
    return Finding(
        id=f"test:{severity}",
        title=f"{severity} finding",
        severity=severity,  # type: ignore[arg-type]
        confidence=confidence,
        description="test",
        detector="test",
    )


class TestDetermineVerdict:
    def test_empty_looks_ok(self):
        assert determine_verdict([]) == "looks_ok"

    def test_high_blocks(self):
        assert determine_verdict([_finding("high", 0.9)]) == "block"

    def test_critical_blocks(self):
        assert determine_verdict([_finding("critical", 0.9)]) == "block"

    def test_medium_reviews(self):
        assert determine_verdict([_finding("medium", 0.9)]) == "review"

    def test_low_info_looks_ok(self):
        assert determine_verdict([_finding("low", 0.9), _finding("info", 0.9)]) == "looks_ok"

    def test_high_overrides_medium(self):
        assert determine_verdict([_finding("medium", 0.9), _finding("high", 0.9)]) == "block"


class TestCalculateScore:
    def test_empty_zero(self):
        assert calculate_score([]) == 0.0

    def test_high_positive(self):
        score = calculate_score([_finding("high", 0.9)])
        assert score > 0.0
        assert score == pytest.approx(80.0)

    def test_medium_positive(self):
        score = calculate_score([_finding("medium", 0.75)])
        assert score > 0.0
        assert score == pytest.approx(50.0)

    def test_high_greater_than_medium(self):
        high = calculate_score([_finding("high", 0.9)])
        medium = calculate_score([_finding("medium", 0.9)])
        assert high > medium

    def test_mixed_score_between_extremes(self):
        score = calculate_score(
            [_finding("high", 1.0), _finding("info", 1.0), _finding("info", 1.0)]
        )
        # Should stay close to the high base score; extra infos add a tiny bonus.
        assert 80.0 < score < 85.0

    def test_no_zero_score_with_high_findings(self):
        for conf in (1.0, 0.5, 0.1):
            score = calculate_score([_finding("high", conf)])
            assert score > 0.0, f"high confidence {conf} produced zero score"

    def test_score_capped_below_100(self):
        many_critical = [_finding("critical", 1.0) for _ in range(20)]
        assert calculate_score(many_critical) <= 99.0
        assert calculate_score(many_critical) > 95.0

    def test_critical_dominates_info_findings(self):
        """A critical must stay >= 90 even when buried in many low findings."""
        findings = [_finding("critical", 1.0)] + [_finding("info", 1.0) for _ in range(10)]
        score = calculate_score(findings)
        assert score >= 90.0

    def test_critical_alone_scores_high(self):
        score = calculate_score([_finding("critical", 1.0)])
        assert score >= 90.0

    def test_severity_monotonicity(self):
        """Higher severity must produce a higher score at equal confidence."""
        severities = ["info", "low", "medium", "high", "critical"]
        scores = [calculate_score([_finding(sev, 1.0)]) for sev in severities]
        assert scores == sorted(scores)
        assert len(set(scores)) == len(scores)

    def test_confidence_monotonicity(self):
        """Higher confidence must produce a higher (or equal) score for the same severity."""
        for severity in ("critical", "high", "medium", "low"):
            # Use a pair so the confidence of the second finding changes the bonus.
            scores = [
                calculate_score([_finding(severity, 1.0), _finding(severity, conf)])
                for conf in (0.25, 0.5, 0.75, 1.0)
            ]
            assert scores == sorted(scores)
