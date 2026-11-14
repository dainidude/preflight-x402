"""Tests for the generated x402 discovery document and its router."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preflight.api import discovery, payments


def _cfg(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "api_url": "https://preflight--preflight-api-api.us-east.modal.run",
        "network": "base",
        "usdc_asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "pay_to_address": "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41",
        "facilitator_url": "https://x402.org/facilitator",
        "cdp_api_key_id": "cdp-id",
        "cdp_api_key_secret": "cdp-secret",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _amount(price: str) -> str:
    return str(int(Decimal(price.lstrip("$")) * Decimal(1_000_000)))


def test_build_derives_payment_and_every_route_price() -> None:
    doc = discovery.build_x402_discovery(_cfg())

    assert doc["resource"] == "https://preflight--preflight-api-api.us-east.modal.run"
    assert doc["x402Version"] == 2
    assert doc["payment"]["pay_to"] == "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41"
    assert doc["payment"]["network"] == "base"
    assert doc["payment"]["network_caip"] == "eip155:8453"
    assert doc["payment"]["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert doc["payment"]["facilitator"] == discovery.CDP_FACILITATOR_URL

    assert set(doc["pricing"]) == set(payments.ROUTE_PRICES)
    for route, (price, description) in payments.ROUTE_PRICES.items():
        entry = doc["pricing"][route]
        assert entry["price"] == price
        assert entry["amount"] == _amount(price)
        assert entry["description"] == description
        assert entry["network"] == "eip155:8453"
        assert entry["asset"] == doc["payment"]["asset"]
        assert entry["scheme"] == "exact"

    assert doc["pricing"]["POST /v1/token-risk"]["price"] == "$0.05"
    assert doc["pricing"]["POST /v1/token-risk"]["amount"] == "50000"
    assert doc["pricing"]["POST /v1/bundle"]["price"] == "$0.15"
    assert doc["pricing"]["POST /v1/bundle"]["amount"] == "150000"
    assert doc["endpoints"]["GET /v1/verify"] == "attestation verification (free)"
    assert "preflight_bundle" in doc["mcp"]["tools"]
    assert "preflight_verify" in doc["mcp"]["tools"]


def test_facilitator_falls_back_without_cdp_credentials() -> None:
    cfg = _cfg(cdp_api_key_id=None, cdp_api_key_secret=None)
    doc = discovery.build_x402_discovery(cfg)
    assert doc["payment"]["facilitator"] == "https://x402.org/facilitator"


def test_generated_matches_static_reference() -> None:
    static = json.loads(discovery.STATIC_DISCOVERY_PATH.read_text(encoding="utf-8"))
    generated = discovery.build_x402_discovery(_cfg())

    assert set(generated) == set(static)
    assert generated == static


def test_router_serves_generated_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    monkeypatch.setattr(discovery, "config", lambda: cfg)
    app = FastAPI()
    app.include_router(discovery.router)

    with TestClient(app) as client:
        response = client.get("/.well-known/x402.json")

    assert response.status_code == 200
    data = response.json()
    assert data["payment"]["pay_to"] == cfg.pay_to_address
    assert data["pricing"]["POST /v1/token-risk"]["amount"] == "50000"
    assert data["pricing"]["POST /v1/bundle"]["amount"] == "150000"
    assert data["pricing"]["POST /v1/scan"]["amount"] == "10000"