"""Tests for the paying-agent demo client (offline; network tests marked)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# ``client/`` is a standalone demo dir, not a package; import it by path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

import pay_client  # noqa: E402

from x402.http.utils import decode_payment_signature_header, safe_base64_encode  # noqa: E402
from x402.schemas import PaymentRequirements, PaymentRequired, SettleResponse  # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41"
# Deterministic throwaway key for offline tests ONLY — never a funded wallet.
PRIVATE_KEY = "0x" + "33" * 32
API_URL = "http://127.0.0.1:8000"

REPORT: dict[str, Any] = {
    "verdict": "block",
    "score": 92.5,
    "findings": [
        {
            "id": "reentrancy-no-eth",
            "title": "Reentrancy on withdraw()",
            "severity": "critical",
            "confidence": 0.98,
            "description": "External call before state update",
            "snippet": None,
            "detector": "slither",
            "reference": None,
        }
    ],
    "contract_name": "VulnerableVault",
    "solc_version": "0.8.20",
    "scanner_versions": {"slither": "0.10.4"},
    "scanned_at": "2026-11-10T00:00:00Z",
    "source_sha256": "abc123",
    "attestation": None,
    "notes": [],
}


SIM_REPORT: dict[str, Any] = {
    "verdict": "block",
    "score": 85.0,
    "findings": [
        {
            "id": "sim:revert",
            "title": "Simulated call reverts",
            "severity": "high",
            "confidence": 1.0,
            "description": "ERC20: transfer amount exceeds balance",
            "snippet": None,
            "detector": "heuristic:simulation",
            "reference": None,
        }
    ],
    "to": USDC,
    "from_address": "0x000000000000000000000000000000000000dEaD",
    "value_wei": "0",
    "reverted": True,
    "revert_reason": "ERC20: transfer amount exceeds balance",
    "gas_used": "24312",
    "simulated_at": "2026-11-12T00:00:00Z",
    "simulator": "rpc-ethcall:v1",
    "attestation": None,
    "notes": [],
}


def _requirements() -> list[PaymentRequirements]:
    return [
        PaymentRequirements(
            scheme="exact",
            network="eip155:8453",
            asset=USDC,
            amount="10000",
            pay_to=PAY_TO,
            max_timeout_seconds=60,
            extra={},
        )
    ]


def _accepts_json(requirements: list[PaymentRequirements]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json", exclude_none=True) for r in requirements]


def _make_402() -> httpx.Response:
    """A 402 shaped like the Preflight API: bare array in the header, V1 body."""
    return _make_402_amount("10000")


def _make_402_amount(amount: str) -> httpx.Response:
    """A 402 with an explicit amount, used to exercise the per-route prices."""
    requirements = _requirements()
    requirements[0].amount = amount
    accepts = _accepts_json(requirements)
    header = safe_base64_encode(json.dumps(accepts))
    body = {"x402Version": 1, "error": "Payment required", "accepts": accepts}
    return httpx.Response(402, json=body, headers={"PAYMENT-REQUIRED": header})


def _settle_header() -> str:
    settle = SettleResponse(
        success=True,
        payer="0xc5925C971f5628ba1E195C83Ce83317A70b3f30B",
        transaction="0x" + "ab" * 32,
        network="eip155:8453",
        amount="10000",
    )
    return safe_base64_encode(settle.model_dump_json())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_payment_required_bare_array_header() -> None:
    accepts = _accepts_json(_requirements())
    header = safe_base64_encode(json.dumps(accepts))
    parsed = pay_client.parse_payment_required(
        {"PAYMENT-REQUIRED": header},
        b"",
    )
    assert parsed.x402_version == 2
    assert parsed.accepts[0].network == "eip155:8453"
    assert parsed.accepts[0].amount == "10000"
    assert parsed.accepts[0].asset == USDC


def test_parse_payment_required_v1_body() -> None:
    accepts = _accepts_json(_requirements())
    body = json.dumps({"x402Version": 1, "error": "nope", "accepts": accepts}).encode()
    parsed = pay_client.parse_payment_required({}, body)
    assert parsed.accepts[0].pay_to == PAY_TO
    assert parsed.accepts[0].scheme == "exact"


# ---------------------------------------------------------------------------
# Payment construction (offline signing)
# ---------------------------------------------------------------------------


def test_create_payment_signs_correct_amount() -> None:
    payment_required = PaymentRequired(x402_version=2, accepts=_requirements())
    payment = pay_client.create_payment(payment_required, PRIVATE_KEY)
    assert payment.x402_version == 2
    assert payment.accepted.amount == "10000"
    assert payment.accepted.network == "eip155:8453"
    assert "signature" in payment.payload
    assert "authorization" in payment.payload


def test_payment_headers_roundtrip() -> None:
    payment_required = PaymentRequired(x402_version=2, accepts=_requirements())
    payment = pay_client.create_payment(payment_required, PRIVATE_KEY)
    headers = pay_client.payment_headers(payment)
    assert "PAYMENT-SIGNATURE" in headers
    decoded = decode_payment_signature_header(headers["PAYMENT-SIGNATURE"])
    assert decoded.accepted.amount == "10000"


# ---------------------------------------------------------------------------
# Full 402 -> pay -> retry exchange (mocked HTTP)
# ---------------------------------------------------------------------------


def test_scan_paid_full_flow() -> None:
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if "PAYMENT-SIGNATURE" in request.headers:
            return httpx.Response(
                200,
                json=REPORT,
                headers={"PAYMENT-RESPONSE": _settle_header()},
            )
        return _make_402()

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    report, settle = pay_client.scan_paid(
        API_URL, pay_client.SAMPLE_SOURCE, "VulnerableVault", PRIVATE_KEY, http_client=http_client
    )

    assert len(requests_seen) == 2
    assert "PAYMENT-SIGNATURE" not in requests_seen[0].headers
    assert "PAYMENT-SIGNATURE" in requests_seen[1].headers

    assert report["verdict"] == "block"
    assert settle is not None
    assert settle["success"] is True
    assert settle["transaction"].startswith("0x")


def test_scan_paid_no_402_returns_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=REPORT)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    report, settle = pay_client.scan_paid(
        API_URL, pay_client.SAMPLE_SOURCE, None, PRIVATE_KEY, http_client=http_client
    )
    assert report["verdict"] == "block"
    assert settle is None


def test_scan_dev_sends_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPORT)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    report = pay_client.scan_dev(API_URL, pay_client.SAMPLE_SOURCE, None, http_client=http_client)
    assert report["verdict"] == "block"
    assert seen[0].headers.get("X-Preflight-Dev") == "bypass"


# ---------------------------------------------------------------------------
# Multi-route payloads (offline, pure)
# ---------------------------------------------------------------------------


def test_route_table_prices() -> None:
    assert pay_client.ROUTES["scan"].path == "/v1/scan"
    assert pay_client.ROUTES["scan"].amount_base_units == "10000"
    assert pay_client.ROUTES["sim"].path == "/v1/sim"
    assert pay_client.ROUTES["sim"].amount_base_units == "50000"
    assert pay_client.ROUTES["sim-deep"].path == "/v1/sim/deep"
    assert pay_client.ROUTES["sim-deep"].amount_base_units == "250000"
    assert pay_client.ROUTES["sim-deep"].price == "$0.25"
    assert pay_client.ROUTES["project"].path == "/v1/scan/project"
    assert pay_client.ROUTES["project"].amount_base_units == "100000"
    assert pay_client.ROUTES["address-risk"].path == "/v1/address-risk"
    assert pay_client.ROUTES["address-risk"].amount_base_units == "5000"


def test_erc20_transfer_calldata_encoding() -> None:
    calldata = pay_client.erc20_transfer_calldata(PAY_TO, 1)
    assert calldata.startswith("0xa9059cbb")
    assert len(calldata) == 10 + 64 + 64
    assert calldata[10:74].endswith(PAY_TO[2:].lower())
    assert calldata[74:] == format(1, "064x")


def test_sim_demo_payload_is_zero_value_transfer() -> None:
    payload = pay_client.sim_payload()
    assert payload["to"] == USDC
    assert payload["value_wei"] == "0"
    assert payload["data"].startswith("0xa9059cbb")
    # The demo amount must exceed any realistic burner balance so the call
    # reverts (a valid sim:revert finding).
    amount = int(payload["data"][74:], 16)
    assert amount == pay_client.DEMO_TRANSFER_AMOUNT
    assert amount > 20 * 10**6


def test_sim_payload_custom() -> None:
    payload = pay_client.sim_payload(
        to=PAY_TO, data="0xdeadbeef", value_wei="42", from_address=PAY_TO
    )
    assert payload == {
        "to": PAY_TO,
        "data": "0xdeadbeef",
        "value_wei": "42",
        "from_address": PAY_TO,
    }


def test_sim_deep_route_payload_shape() -> None:
    route = pay_client.ROUTES["sim-deep"]
    assert route.path == "/v1/sim/deep"
    assert route.price == "$0.25"
    assert route.amount_base_units == "250000"

    payload = pay_client.build_payload(
        "sim-deep", to=PAY_TO, data="0xdeadbeef", value_wei="7"
    )
    # Same SimRequest body as quick sim; /v1/sim/deep forces mode=deep server-side.
    assert payload == {"to": PAY_TO, "data": "0xdeadbeef", "value_wei": "7"}
    assert "mode" not in payload
    assert pay_client.build_payload("sim-deep")["to"] == USDC


def test_sim_payload_rejects_bad_address_and_data() -> None:
    with pytest.raises(ValueError):
        pay_client.sim_payload(to="0xnope")
    with pytest.raises(ValueError):
        pay_client.sim_payload(to=PAY_TO, data="deadbeef")


def test_build_payload_all_routes() -> None:
    assert pay_client.build_payload("scan", source="contract X {}") == {
        "source": "contract X {}"
    }
    assert pay_client.build_payload(
        "project", files={"X.sol": "contract X {}"}, contract_name="X"
    ) == {"files": {"X.sol": "contract X {}"}, "contract_name": "X"}
    assert pay_client.build_payload("sim")["to"] == USDC
    assert pay_client.build_payload("sim-deep")["to"] == USDC
    assert pay_client.build_payload("address-risk", address=PAY_TO) == {"address": PAY_TO}
    with pytest.raises(ValueError):
        pay_client.build_payload("address-risk")


@pytest.mark.parametrize("route_name", ["sim", "sim-deep", "project", "address-risk"])
def test_post_paid_route_amount_and_path(route_name: str) -> None:
    route = pay_client.ROUTES[route_name]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "PAYMENT-SIGNATURE" in request.headers:
            decoded = decode_payment_signature_header(request.headers["PAYMENT-SIGNATURE"])
            assert decoded.accepted.amount == route.amount_base_units
            body = SIM_REPORT if route_name.startswith("sim") else REPORT
            return httpx.Response(
                200, json=body, headers={"PAYMENT-RESPONSE": _settle_header()}
            )
        return _make_402_amount(route.amount_base_units)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = pay_client.build_payload(
        route_name,
        source=pay_client.SAMPLE_SOURCE,
        files={"Vault.sol": pay_client.SAMPLE_SOURCE},
        address=PAY_TO,
    )
    report, settle = pay_client.post_paid(
        API_URL, route.path, payload, PRIVATE_KEY, http_client=http_client
    )

    assert len(seen) == 2
    assert seen[0].url.path == route.path
    assert "PAYMENT-SIGNATURE" in seen[1].headers
    assert report["verdict"] == "block"
    assert settle is not None and settle["success"] is True


def test_post_dev_route_sends_header_and_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SIM_REPORT)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    report = pay_client.post_dev(
        API_URL, "/v1/sim", pay_client.sim_payload(), http_client=http_client
    )
    assert report["reverted"] is True
    assert seen[0].url.path == "/v1/sim"
    assert seen[0].headers.get("X-Preflight-Dev") == "bypass"


def test_paid_exchange_rejected_payment_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" in request.headers:
            return httpx.Response(402, json={"error": "payment verification failed"})
        return _make_402()

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="payment rejected"):
        pay_client.post_paid(
            API_URL,
            "/v1/scan",
            pay_client.scan_payload(pay_client.SAMPLE_SOURCE),
            PRIVATE_KEY,
            http_client=http_client,
        )


# ---------------------------------------------------------------------------
# .env key handling
# ---------------------------------------------------------------------------


def test_load_or_create_key_persists(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    key1, created = pay_client.load_or_create_key(env_file)
    assert created is True
    key2, created_again = pay_client.load_or_create_key(env_file)
    assert created_again is False
    assert key1 == key2
    # The saved key is a valid hex private key.
    from eth_account import Account

    account = Account.from_key(key1)
    assert account.address.startswith("0x")


# ---------------------------------------------------------------------------
# Real network (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_get_usdc_balance_network() -> None:
    balance = pay_client.get_usdc_balance(
        "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41",
        pay_client.DEFAULT_RPC_URL,
    )
    assert isinstance(balance, int)
