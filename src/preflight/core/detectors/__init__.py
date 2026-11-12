"""Custom detectors package for Preflight."""

from preflight.core.detectors.custom import (
    DetectorContext,
    detect_fee_on_transfer,
    detect_honeypot_patterns,
    detect_missing_zero_address_checks,
    detect_oracle_freshness,
    detect_unguarded_initializer,
    run_all_custom_detectors,
)

__all__ = [
    "DetectorContext",
    "detect_fee_on_transfer",
    "detect_honeypot_patterns",
    "detect_missing_zero_address_checks",
    "detect_oracle_freshness",
    "detect_unguarded_initializer",
    "run_all_custom_detectors",
]