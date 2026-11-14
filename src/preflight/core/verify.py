"""Public attestation verification helpers.

E1: wraps the arbiter-owned EIP-712 signer so buyers can verify a report
off-chain without trusting Preflight's server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from preflight.attestation import verify_report


def verify_attestation(digest: str, attestation: dict[str, Any]) -> dict[str, Any]:
    """Verify an EIP-712 attestation against a source digest.

    Args:
        digest: The report's ``source_sha256`` (the value that was signed).
        attestation: The attestation dict returned by ``sign_report``,
            containing ``message``, ``signature`` and ``signer``.

    Returns:
        {"valid": true, "signer": ..., "verdict": ..., "source_sha256": ...,
         "checked_at": ...} on success, or {"valid": false, "reason": ...}.
    """
    if not isinstance(attestation, dict):
        return {"valid": False, "reason": "attestation must be a JSON object"}
    if "message" not in attestation or "signature" not in attestation:
        return {
            "valid": False,
            "reason": "attestation must contain 'message' and 'signature'",
        }

    message = attestation.get("message", {})
    report = {
        "source_sha256": digest,
        "verdict": message.get("verdict", ""),
    }

    try:
        valid = verify_report(report, attestation)
    except Exception as exc:
        return {"valid": False, "reason": f"verification failed: {exc}"}

    result: dict[str, Any] = {
        "valid": valid,
        "signer": attestation.get("signer"),
        "verdict": message.get("verdict"),
        "source_sha256": digest,
        "checked_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    if not valid:
        result["reason"] = (
            "signature does not match the digest/verdict, or the report shape is invalid"
        )
    return result
