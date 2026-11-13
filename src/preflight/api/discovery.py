"""Generated x402 discovery document.

The static ``public/.well-known/x402.json`` file is kept as a human-readable
reference/fallback, but the live route must not drift from the code: prices
come from :data:`preflight.api.payments.ROUTE_PRICES`, payment details from
:class:`preflight.config.Config`, and the canonical URL from ``cfg.api_url``.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from preflight.api import payments
from preflight.config import Config, config

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
STATIC_DISCOVERY_PATH = (
    Path(__file__).resolve().parents[3] / "public" / ".well-known" / "x402.json"
)

# Token-risk is being wired by the arbiter in this wave. Keep discovery complete
# even if ROUTE_PRICES has not landed yet; ROUTE_PRICES wins when present.
_TOKEN_RISK_ROUTE = "POST /v1/token-risk"
_TOKEN_RISK_FALLBACK = (
    "$0.05",
    "Token risk: honeypot transfer probe, transfer tax, and owner privileges",
)


def _routes() -> dict[str, tuple[str, str]]:
    """Paid routes and their (price, description), with a token-risk fallback."""
    routes = dict(payments.ROUTE_PRICES)
    routes.setdefault(_TOKEN_RISK_ROUTE, _TOKEN_RISK_FALLBACK)
    return routes


def _amount(price: str) -> str:
    """Convert ``'$0.005'`` to USDC base units (6 decimals) exactly."""
    return str(int(Decimal(price.lstrip("$")) * Decimal(1_000_000)))


def _facilitator(cfg: Config) -> str:
    """The facilitator actually used by the resource server.

    CDP credentials switch payments to the Coinbase facilitator; without them
    the configured public facilitator is used.
    """
    if getattr(cfg, "cdp_api_key_id", None) and getattr(cfg, "cdp_api_key_secret", None):
        return CDP_FACILITATOR_URL
    return getattr(cfg, "facilitator_url", "https://x402.org/facilitator")


def _template_sections() -> dict[str, Any]:
    """Static Bazaar/MCP sections that describe request/response shapes.

    These are discovery metadata, not money values. Reading the reference file
    keeps the document's structure stable; pricing and payment fields below are
    always generated from code.
    """
    try:
        data = json.loads(STATIC_DISCOVERY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {key: copy.deepcopy(data[key]) for key in ("extensions",) if key in data}


def build_x402_discovery(cfg: Config | None = None) -> dict[str, Any]:
    """Build the x402 discovery document for ``cfg``."""
    cfg = cfg or config()
    network = getattr(cfg, "network", "base")
    network_caip = payments.NETWORK_CAIP[network]
    asset = getattr(cfg, "usdc_asset", "")
    pay_to = getattr(cfg, "pay_to_address", None) or ""
    routes = _routes()

    pricing: dict[str, dict[str, str]] = {}
    endpoints: dict[str, str] = {
        "GET /": "service info and price list (free)",
        "GET /health": "health check (free)",
        "GET /metrics": "revenue and usage metrics (free)",
    }
    for route, (price, description) in routes.items():
        pricing[route] = {
            "price": price,
            "amount": _amount(price),
            "description": description,
            "network": network_caip,
            "asset": asset,
            "scheme": "exact",
        }
        endpoints[route] = f"{description} ({price})"
    endpoints.update(
        {
            "GET /llms.txt": "LLM-facing service description",
            "GET /.well-known/x402.json": "this file",
        }
    )

    doc: dict[str, Any] = {
        "x402Version": 2,
        "resource": str(getattr(cfg, "api_url", "")).rstrip("/"),
        "service": "Preflight",
        "service_name": "Preflight",
        "description": (
            "Pay-per-call smart-contract & transaction pre-flight security API. "
            "Submit Solidity source or a transaction for simulation and receive a "
            "severity-scored, EIP-712-attested verdict, paid per call via x402 "
            "(HTTP 402 + USDC on Base mainnet)."
        ),
        "version": "0.1.0",
        "protocol": "x402",
        "tags": ["security", "solidity", "smart-contracts", "simulation", "audit"],
        "payment": {
            "scheme": "exact",
            "network": network,
            "network_caip": network_caip,
            "asset": asset,
            "asset_symbol": "USDC",
            "asset_decimals": 6,
            "facilitator": _facilitator(cfg),
            "pay_to": pay_to,
            "max_timeout_seconds": 60,
        },
        "pricing": pricing,
        "endpoints": endpoints,
    }
    doc["extensions"] = _template_sections().get("extensions", {"bazaar": {}})

    tools: dict[str, str] = {}
    if "POST /v1/scan" in routes:
        tools["preflight_check"] = f"scan Solidity source ({routes['POST /v1/scan'][0]})"
    if "POST /v1/sim" in routes or "POST /v1/sim/deep" in routes:
        tools["preflight_sim"] = "simulate a transaction (quick or deep)"
    if _TOKEN_RISK_ROUTE in routes:
        tools["preflight_token_risk"] = (
            f"check a token before trading ({routes[_TOKEN_RISK_ROUTE][0]})"
        )
    tools["preflight_pricing"] = "free price list"
    doc["mcp"] = {"tools": tools, "transport": "stdio | streamable-http"}
    return doc


router = APIRouter()


@router.get("/.well-known/x402.json", include_in_schema=False)
async def x402_discovery() -> JSONResponse:
    """Serve the generated discovery document as JSON."""
    return JSONResponse(build_x402_discovery(config()))