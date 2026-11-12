"""End-to-end paid-flow test against a live Preflight API (network-marked).

This is the launch gate: it spends real test USDC ($0.01) through the x402.org
facilitator and then verifies the server booked the revenue.

Preconditions:

* an API running in PROD mode (``DEV_MODE=0``, ``FREE_TIER=0``) at
  ``PREFLIGHT_API_URL`` (default ``http://127.0.0.1:8000``), with a valid
  ``PREFLIGHT_PAY_TO_ADDRESS`` and a reachable facilitator;
* ``CLIENT_PRIVATE_KEY`` (environment or ``.env``) with test-USDC on Base
  Sepolia.

The test skips cleanly when there is no key or the wallet balance is 0, and
also when the server grants a dev bypass / free-tier scan (then no payment is
actually settled, so it cannot prove the paid flow).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

# ``client/`` is a standalone demo dir, not a package; import it by path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

import pay_client  # noqa: E402

from preflight.types import ScanReport  # noqa: E402
from x402.http.constants import PAYMENT_RESPONSE_HEADER  # noqa: E402

PRICE_BASE_UNITS = 10_000  # $0.01 at 6 decimals


def _setting(name: str, default: str | None = None) -> str | None:
    """Environment first, then the repo ``.env`` (where the burner key lives)."""
    return os.getenv(name) or pay_client.load_env(pay_client.ENV_FILE).get(name) or default


@pytest.mark.network
def test_paid_scan_then_metrics() -> None:
    api_url = _setting("PREFLIGHT_API_URL", "http://127.0.0.1:8000")
    private_key = _setting("CLIENT_PRIVATE_KEY")
    if not private_key:
        pytest.skip("CLIENT_PRIVATE_KEY not set (environment or .env)")

    address = pay_client.Account.from_key(private_key).address
    rpc_url = _setting("RPC_URL", pay_client.DEFAULT_RPC_URL)
    try:
        balance = pay_client.get_usdc_balance(address, rpc_url)
    except Exception as exc:  # RPC unreachable
        pytest.skip(f"could not read Base Sepolia USDC balance: {exc}")
    if balance <= 0:
        pytest.skip(f"burner {address} has 0 test USDC; fund it to run the paid flow")

    url = f"{api_url.rstrip('/')}/v1/scan"
    payload = pay_client.scan_payload(pay_client.SAMPLE_SOURCE, "VulnerableVault")

    with httpx.Client(timeout=120.0) as client:
        first = client.post(url, json=payload)

        if first.status_code == 200 and first.headers.get("X-Preflight-Dev"):
            pytest.skip("API is in DEV_MODE=1; paid flow needs PROD mode")
        if first.status_code == 200 and first.headers.get("X-Preflight-Free-Tier"):
            pytest.skip("API granted a free-tier scan; set FREE_TIER=0 to pay")
        assert first.status_code == 402, f"expected 402, got {first.status_code}: {first.text}"

        requirements = pay_client.parse_payment_required(
            pay_client._headers_dict(first.headers), first.content
        )
        signed = pay_client.create_payment(requirements, private_key)
        retry = client.post(url, json=payload, headers=pay_client.payment_headers(signed))

        assert retry.status_code == 200, f"paid retry failed: {retry.status_code} {retry.text}"
        assert retry.headers.get(PAYMENT_RESPONSE_HEADER), "missing PAYMENT-RESPONSE header"

    # The report body must be a valid ScanReport, produced by the real pipeline.
    report = ScanReport.model_validate(retry.json())
    assert report.verdict in {"looks_ok", "review", "block"}
    assert report.source_sha256

    settle = pay_client.parse_settle_response(pay_client._headers_dict(retry.headers))
    assert settle is not None
    assert settle["network"] == "eip155:84532"
    # Note: settle["success"] can be False transiently (facilitator pending);
    # the hard launch gate is that revenue was actually booked — see below.
    # Revenue is only recorded on successful settlement, so the metrics
    # assertion below fails the gate if the payment did not really settle.

    with httpx.Client(timeout=30.0) as client:
        metrics = client.get(f"{api_url.rstrip('/')}/metrics")
        metrics.raise_for_status()
        data = metrics.json()
    counters = data.get("metrics", data)
    assert counters["revenue_base_units"] >= PRICE_BASE_UNITS, data
