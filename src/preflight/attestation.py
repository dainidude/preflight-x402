"""EIP-712 attestations over scan reports.

Signs a digest of the report so a buyer can prove what Preflight said about a
specific source (identified by source_sha256) at a point in time. Returns None
when PREFLIGHT_ATTEST_KEY is unset — attestation is optional, the verdict
stands alone.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from .config import config

_DOMAIN_NAME = "Preflight"
_DOMAIN_VERSION = "1"

_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
    ],
    "ReportAttestation": [
        {"name": "sourceSha256", "type": "bytes32"},
        {"name": "verdict", "type": "string"},
        {"name": "findingsRoot", "type": "bytes32"},
        {"name": "scannedAt", "type": "string"},
        {"name": "signedAt", "type": "uint256"},
    ],
}


def _findings_root(findings: list[dict[str, Any]]) -> bytes:
    """Deterministic merkle-ish digest over the findings list."""
    h = hashlib.sha256()
    for f in sorted(findings, key=lambda x: (x.get("id", ""), x.get("severity", ""))):
        h.update(
            "|".join(
                [
                    str(f.get("id", "")),
                    str(f.get("severity", "")),
                    str(f.get("confidence", "")),
                ]
            ).encode()
        )
    return h.digest()


def sign_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """Return an EIP-712 attestation dict for a ScanReport-shaped dict, or None."""
    key = config().attest_key_hex
    if not key:
        return None
    account = Account.from_key(key)
    message = {
        "domain": {"name": _DOMAIN_NAME, "version": _DOMAIN_VERSION},
        "types": _TYPES,
        "primaryType": "ReportAttestation",
        "message": {
            "sourceSha256": "0x" + report.get("source_sha256", hashlib.sha256(b"").hexdigest()),
            "verdict": str(report.get("verdict", "")),
            "findingsRoot": "0x" + _findings_root(report.get("findings", [])).hex(),
            "scannedAt": str(report.get("scanned_at", "")),
            "signedAt": int(time.time()),
        },
    }
    signed = account.sign_message(encode_typed_data(full_message=message))
    return {
        "signer": account.address,
        "signature": signed.signature.hex(),
        "scheme": "eip712:report-attestation:v1",
        "message": message["message"],
    }


def verify_report(report: dict[str, Any], attestation: dict[str, Any]) -> bool:
    """Verify an attestation against a report dict (best-effort, local)."""
    try:
        message = {
            "domain": {"name": _DOMAIN_NAME, "version": _DOMAIN_VERSION},
            "types": _TYPES,
            "primaryType": "ReportAttestation",
            "message": attestation["message"],
        }
        recovered = Account.recover_message(
            encode_typed_data(full_message=message), signature=attestation["signature"]
        )
    except Exception:
        return False
    if recovered != attestation.get("signer"):
        return False
    msg = attestation["message"]
    expected = "0x" + str(report.get("source_sha256", "")).lower().removeprefix("0x")
    return msg.get("sourceSha256", "").lower() == expected and msg.get("verdict") == report.get("verdict")
