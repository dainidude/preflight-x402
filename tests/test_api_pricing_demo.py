"""Repricing ($0.02 token-risk / $0.03 bundle) + free demo route tests.

Arbiter wave 7: the conversion surface. Offline — core analysis is
monkeypatched, no RPC, no facilitator.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from preflight.api import app as app_mod  # noqa: E402
from preflight.api import payments as pay_mod  # noqa: E402
from preflight.api.app import app  # noqa: E402
from preflight.api.payments import ROUTE_PRICES  # noqa: E402
from preflight.config import Config  # noqa: E402
from preflight.types import ScanReport  # noqa: E402


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("FREE_TIER", "1")
    monkeypatch.setenv("PREFLIGHT_PAY_TO_ADDRESS", "0x" + "11" * 20)
    Config.reload()


def _fake_token_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "_get_token_risk", lambda: _fake_run)
    pay_mod.usage = pay_mod.UsageTracker()


async def _fake_run(address: str) -> ScanReport:
    return ScanReport(
        verdict="looks_ok", score=0.0, findings=[], contract_name=None,
        solc_version="0.8.20", scanner_versions={"token": "v1"},
        scanned_at="2026-11-16T00:00:00Z", source_sha256="a" * 64,
    )


def test_token_risk_repriced_to_2_cents() -> None:
    assert ROUTE_PRICES["POST /v1/token-risk"][0] == "$0.02"
    assert pay_mod._route_amount("POST /v1/token-risk") == 20_000


def test_bundle_repriced_to_3_cents() -> None:
    assert ROUTE_PRICES["POST /v1/bundle"][0] == "$0.03"


def test_openapi_paid_paths_derived_from_route_prices(demo_env: None) -> None:
    """The OpenAPI x-payment-info table must never drift from ROUTE_PRICES."""
    assert app_mod._PAID_PATHS["/v1/token-risk"] == "0.02"
    assert app_mod._PAID_PATHS["/v1/bundle"] == "0.03"


def test_402_hint_teaches_demo_route(
    demo_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pay_mod, "build_requirements",
        lambda route, cfg=None: _make_requirements(),
    )
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post("/v1/token-risk", json={"address": "0x" + "11" * 20})
        assert r.status_code == 402
        assert "/v1/token-risk/demo?address=" in r.json()["hint"]
        # the 402 must charge the NEW price
        assert r.json()["accepts"][0]["amount"] == "20000"


def _make_requirements() -> list[object]:
    from x402.schemas.payments import PaymentRequirements

    return [
        PaymentRequirements(
            scheme="exact", network="eip155:84532",
            asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            amount="20000", pay_to="0x" + "11" * 20, max_timeout_seconds=60,
            extra={"name": "USDC", "version": "2", "description": "token risk"},
        )
    ]


def test_demo_returns_full_report_free(
    demo_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_token_risk(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/v1/token-risk/demo", params={"address": "0x" + "11" * 20})
        assert r.status_code == 200
        body = r.json()
        assert body["demo"] is True
        assert body["verdict"] == "looks_ok"
        assert "attestation" in body
        assert any("FULL report" in n for n in body["notes"])
        assert any("$0.02" in n for n in body["notes"])


def test_demo_quoted_per_day(
    demo_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_token_risk(monkeypatch)
    with TestClient(app) as c:
        for _ in range(pay_mod.DEMO_DAILY):
            r = c.get("/v1/token-risk/demo", params={"address": "0x" + "11" * 20})
            assert r.status_code == 200
        r = c.get("/v1/token-risk/demo", params={"address": "0x" + "11" * 20})
        assert r.status_code == 429
        assert "X-Preflight-Free" in r.json()["hint"]


def test_demo_requires_address(
    demo_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_token_risk(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/v1/token-risk/demo")
        assert r.status_code == 422


def test_demo_counts_tracked_in_metrics(
    demo_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preflight.api.metrics import collector

    collector._demo_calls_total = 0
    _fake_token_risk(monkeypatch)
    with TestClient(app) as c:
        c.get("/v1/token-risk/demo", params={"address": "0x" + "11" * 20})
    assert collector._demo_calls_total == 1
