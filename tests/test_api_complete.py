"""API payment-flow tests — 402 shape, free tier, rate limit. Arbiter rewrite.

No network: build_requirements and the resource server are monkeypatched, so
the real facilitator is never contacted.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from preflight.api import app as app_mod  # noqa: E402
from preflight.api import payments as pay_mod  # noqa: E402
from preflight.api.app import app  # noqa: E402
from preflight.config import Config  # noqa: E402
from preflight.types import ScanReport  # noqa: E402

REQ_DICT = {
    "scheme": "exact",
    "network": "eip155:84532",
    "maxAmountRequired": "10000",
    "resource": "http://testserver/v1/scan",
    "description": "Smart-contract security scan",
    "mimeType": "application/json",
    "payTo": "0x" + "11" * 20,
    "maxTimeoutSeconds": 60,
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "extra": {"name": "USDC", "version": "2"},
}


class FakeRequirements:
    """Stands in for PaymentRequirements without touching the x402 server."""

    def model_dump(self, **_: object) -> dict:
        return dict(REQ_DICT)


@pytest.fixture()
def prod_client(prod_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(pay_mod, "build_requirements", lambda route, cfg=None: [FakeRequirements()])
    monkeypatch.setattr(app_mod, "_run_analysis", lambda s, cn=None, sv=None: ScanReport(
        verdict="looks_ok", score=0.0, findings=[], contract_name=cn,
        solc_version=sv or "0.8.20", scanner_versions={"slither": "0.11.6"},
        scanned_at="2026-11-10T00:00:00Z", source_sha256="a" * 64,
    ))
    with TestClient(app) as c:
        yield c


def test_unpaid_request_gets_spec_shaped_402(prod_client: TestClient) -> None:
    r = prod_client.post("/v1/scan", json={"source": "contract A {}"})
    assert r.status_code == 402, r.text
    # body is spec-shaped
    body = r.json()
    assert body["x402Version"] == 1
    assert body["accepts"][0]["scheme"] == "exact"
    assert body["accepts"][0]["payTo"].startswith("0x")
    # PAYMENT-REQUIRED header carries base64 JSON of the requirements
    header = r.headers.get("payment-required")
    assert header, "PAYMENT-REQUIRED header missing"
    decoded = json.loads(base64.b64decode(header))
    assert decoded[0]["network"] == "eip155:84532"


def test_malformed_signature_gets_402(prod_client: TestClient) -> None:
    r = prod_client.post(
        "/v1/scan",
        json={"source": "contract A {}"},
        headers={"PAYMENT-SIGNATURE": base64.b64encode(b"not json").decode()},
    )
    assert r.status_code == 402
    assert "invalid payment" in r.json()["error"]


def test_free_tier_grants_then_exhausts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("FREE_TIER", "1")
    Config.reload()
    monkeypatch.setattr(pay_mod, "build_requirements", lambda route, cfg=None: [FakeRequirements()])
    monkeypatch.setattr(app_mod, "_run_analysis", lambda s, cn=None, sv=None: ScanReport(
        verdict="looks_ok", score=0.0, findings=[], contract_name=None,
        solc_version="0.8.20", scanner_versions={"slither": "0.11.6"},
        scanned_at="2026-11-10T00:00:00Z", source_sha256="b" * 64,
    ))
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        for i in range(10):
            r = c.post("/v1/scan", json={"source": f"contract N{i} {{}}"},
                       headers={"X-Preflight-Free": "trial"})
            assert r.status_code == 200, f"free call {i} failed"
            assert r.headers.get("x-preflight-free-tier") == "granted"
        r = c.post("/v1/scan", json={"source": "contract Eleventh {}"},
                   headers={"X-Preflight-Free": "trial"})
        assert r.status_code == 402, "11th call should require payment"


def test_no_header_means_immediate_402(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crawlers/validators never send the trial header — they must see the 402."""
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("FREE_TIER", "1")
    Config.reload()
    monkeypatch.setattr(pay_mod, "build_requirements", lambda route, cfg=None: [FakeRequirements()])
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post("/v1/scan", json={"source": "contract A {}"})
        assert r.status_code == 402, "bare request must be paywalled (free tier is opt-in)"
        assert r.headers.get("x-preflight-free-tier") is None


def test_usage_tracker_rate_limit() -> None:
    t = pay_mod.UsageTracker()
    assert all(t.allow_rate("bob") for _ in range(10))
    assert not t.allow_rate("bob")
    assert t.allow_rate("alice")


def test_free_routes_never_require_payment(prod_client: TestClient) -> None:
    for path in ("/", "/health", "/llms.txt", "/.well-known/x402.json"):
        r = prod_client.get(path)
        assert r.status_code in (200, 404), f"{path} should not be paywalled: {r.status_code}"
