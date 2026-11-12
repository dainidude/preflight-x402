"""x402 payment wiring for the Preflight API — REAL x402 package integration.

Rebuilt by the arbiter (opencode) after the first implementation turned out to
be a mock. Uses the installed `x402` package (v2.27.0):

- `x402ResourceServer` builds payment requirements, verifies the
  PAYMENT-SIGNATURE header via the facilitator, and settles after the handler.
- `HTTPFacilitatorClient` talks to the facilitator (default https://x402.org,
  i.e. testnet verify/settle).
- `ExactEvmServerScheme` implements the `exact` scheme (USDC transfer).

Flow for a paid route:
1. DEV_MODE=1            -> bypass, `X-Preflight-Dev: bypass` header.
2. FREE_TIER allowance   -> serve free, `X-Preflight-Free-Tier: granted`.
3. No PAYMENT-SIGNATURE  -> HTTP 402 with `PAYMENT-REQUIRED` header (base64
                            JSON requirements) + spec-shaped JSON body.
4. Signature present     -> facilitator verify -> serve -> facilitator settle
                            -> `PAYMENT-RESPONSE` header (base64 settle JSON).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import Request, Response

from x402.http.constants import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
)
from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.utils import safe_base64_decode, safe_base64_encode
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import PaymentPayload, PaymentPayloadV1, PaymentRequirements
from x402.server import x402ResourceServer

from preflight.config import Config, config

# Human network name -> CAIP-2 identifier used by the x402 package.
NETWORK_CAIP: dict[str, str] = {
    "base-sepolia": "eip155:84532",
    "base": "eip155:8453",
}

FREE_ROUTES = {
    "GET /",
    "GET /health",
    "GET /metrics",
    "GET /llms.txt",
    "GET /.well-known/x402.json",
}

# route -> (price, description)
ROUTE_PRICES: dict[str, tuple[str, str]] = {
    "POST /v1/scan": ("$0.01", "Smart-contract security scan (static + custom DeFi detectors)"),
    "POST /v1/sim": ("$0.05", "Transaction pre-flight simulation (RPC eth_call)"),
    "POST /v1/scan/project": ("$0.10", "Multi-file project security scan"),
    "POST /v1/address-risk": ("$0.005", "Address risk heuristics"),
}

FREE_TIER_DAILY = 10
RATE_LIMIT_PER_MIN = 10

# Bazaar discovery declarations per route (CDP Bazaar: Python is opt-in —
# every discoverable route must declare its metadata explicitly). Schema +
# example let an agent construct a valid paid call without guessing.
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension  # noqa: E402

ROUTE_BAZAAR: dict[str, dict] = {
    "POST /v1/scan": declare_discovery_extension(
        input={
            "source": "contract Vault { mapping(address => uint256) balances; }",
            "contract_name": "Vault",
        },
        input_schema={
            "properties": {
                "source": {"type": "string", "description": "Solidity source code of the contract to scan"},
                "contract_name": {"type": "string", "description": "Target contract name when the source has several"},
                "solc_version": {"type": "string", "description": "e.g. 0.8.20; auto-detected from pragma when omitted"},
            },
            "required": ["source"],
        },
        body_type="json",
        output=OutputConfig(
            example={
                "verdict": "block",
                "score": 24.7,
                "findings": [{"id": "reentrancy-eth", "severity": "high", "title": "Reentrancy"}],
            }
        ),
    ),
    "POST /v1/sim": declare_discovery_extension(
        input={"to": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "data": "0x", "value_wei": "0"},
        input_schema={
            "properties": {
                "to": {"type": "string", "description": "Target contract address"},
                "data": {"type": "string", "description": "0x-hex calldata (omit for plain value transfer)"},
                "value_wei": {"type": "string", "description": "Wei value, decimal string"},
            },
            "required": ["to"],
        },
        body_type="json",
        output=OutputConfig(example={"verdict": "review", "reverted": True, "revert_reason": "…"}),
    ),
    "POST /v1/scan/project": declare_discovery_extension(
        input={"files": {"Vault.sol": "contract Vault {}"}},
        input_schema={
            "properties": {
                "files": {
                    "type": "object",
                    "description": "Mapping of file name to Solidity source",
                },
                "contract_name": {"type": "string"},
            },
            "required": ["files"],
        },
        body_type="json",
        output=OutputConfig(example={"verdict": "looks_ok", "score": 0.0, "findings": []}),
    ),
    "POST /v1/address-risk": declare_discovery_extension(
        input={"address": "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41"},
        input_schema={
            "properties": {"address": {"type": "string", "description": "EVM address to screen"}},
            "required": ["address"],
        },
        body_type="json",
        output=OutputConfig(example={"verdict": "looks_ok", "findings": []}),
    ),
}


def price_list() -> dict[str, str]:
    return {route: price for route, (price, _) in ROUTE_PRICES.items()}


def _route_amount(route: str) -> int:
    """Price of a route in base units (USDC, 6 decimals): '$0.01' -> 10_000."""
    price = ROUTE_PRICES[route][0].lstrip("$")
    return int(float(price) * 1_000_000)


# ---------------------------------------------------------------------------
# Resource server (lazy: never touches the network in DEV_MODE / unit tests)
# ---------------------------------------------------------------------------

_server: x402ResourceServer | None = None
_requirements_cache: dict[str, list[PaymentRequirements]] = {}


def _resource_server(cfg: Config) -> x402ResourceServer:
    global _server
    if _server is None:
        from x402.extensions.bazaar import bazaar_resource_server_extension

        facilitator = HTTPFacilitatorClient({"url": cfg.facilitator_url})
        _server = x402ResourceServer(facilitator)
        _server.register(NETWORK_CAIP[cfg.network], ExactEvmServerScheme())
        # Bazaar discovery layer: makes this server's routes findable by
        # Bazaar-crawling facilitators (CDP discovery indexes merchants).
        _server.register_extension(bazaar_resource_server_extension)
        _server.initialize()
    return _server


def build_requirements(route: str, cfg: Config | None = None) -> list[PaymentRequirements]:
    """PaymentRequirements for a route (cached; monkeypatch me in tests)."""
    cfg = cfg or config()
    if route not in _requirements_cache:
        from x402.schemas.config import ResourceConfig

        price, description = ROUTE_PRICES[route]
        server = _resource_server(cfg)
        rc = ResourceConfig(
            scheme="exact",
            pay_to=cfg.pay_to_address or "",
            price=price,
            network=NETWORK_CAIP[cfg.network],
            max_timeout_seconds=60,
            extra={"description": description},
        )
        requirements = server.build_payment_requirements(rc)
        # Attach the Bazaar discovery declaration so the 402 advertises it
        # (CDP Bazaar validation requires an extensions.bazaar block).
        for req in requirements:
            req.extensions = dict(ROUTE_BAZAAR.get(route) or {})
        _requirements_cache[route] = requirements
    return _requirements_cache[route]


def parse_payment_header(header: str) -> PaymentPayload | PaymentPayloadV1:
    """Decode the PAYMENT-SIGNATURE header (base64 JSON) into a payload."""
    raw = safe_base64_decode(header)
    try:
        return PaymentPayload.model_validate_json(raw)
    except Exception:
        return PaymentPayloadV1.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Usage tracking: free tier + rate limit (in-memory, per-process)
# ---------------------------------------------------------------------------

class UsageTracker:
    def __init__(self) -> None:
        self._free_day: dict[str, tuple[str, int]] = {}
        self._window: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def fingerprint(request: Request) -> str:
        payer = request.headers.get("x-payer")
        if payer:
            return payer.lower()
        client = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")
        return hashlib.sha256(f"{client}|{ua}".encode()).hexdigest()[:20]

    def free_left(self, ident: str) -> int:
        today = time.strftime("%Y-%m-%d")
        day, used = self._free_day.get(ident, (today, 0))
        if day != today:
            return FREE_TIER_DAILY
        return max(0, FREE_TIER_DAILY - used)

    def consume_free(self, ident: str) -> bool:
        today = time.strftime("%Y-%m-%d")
        day, used = self._free_day.get(ident, (today, 0))
        if day != today:
            used = 0
        if used >= FREE_TIER_DAILY:
            return False
        self._free_day[ident] = (today, used + 1)
        return True

    def allow_rate(self, ident: str) -> bool:
        now = time.time()
        q = self._window[ident]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True


usage = UsageTracker()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

def _payment_required_response(route: str, requirements: list[PaymentRequirements]) -> Response:
    from fastapi.responses import JSONResponse

    accepts = [r.model_dump(mode="json", exclude_none=True) for r in requirements]
    payload = json.dumps(accepts)
    return JSONResponse(
        status_code=402,
        content={
            "x402Version": 1,
            "error": f"Payment required for {route}",
            "accepts": accepts,
        },
        headers={PAYMENT_REQUIRED_HEADER: safe_base64_encode(payload)},
    )


async def x402_payment_middleware(request: Request, call_next: Callable) -> Response:
    from preflight.api.metrics import collector

    route = f"{request.method} {request.url.path}"
    if route in FREE_ROUTES or route not in ROUTE_PRICES:
        return await call_next(request)

    cfg = config()
    request.state.pf_paid = False

    if cfg.dev_mode:
        response = await call_next(request)
        response.headers["X-Preflight-Dev"] = "bypass"
        return response

    ident = usage.fingerprint(request)

    # Free tier is OPT-IN via explicit header so that x402 crawlers/validators
    # (which never send it) always see the required 402 payment challenge.
    free_trial = request.headers.get("x-preflight-free") == "trial"
    if cfg.free_tier and free_trial and usage.consume_free(ident):
        if not usage.allow_rate(ident):
            from fastapi.responses import JSONResponse

            collector.record_rate_limited()
            return JSONResponse(status_code=429, content={"error": "rate limit: 10 req/min"})
        response = await call_next(request)
        response.headers["X-Preflight-Free-Tier"] = "granted"
        collector.record_free(route)
        return response

    signature = request.headers.get(PAYMENT_SIGNATURE_HEADER)
    requirements = build_requirements(route, cfg)

    if not signature:
        collector.record_402(route)
        return _payment_required_response(route, requirements)

    try:
        payload = parse_payment_header(signature)
        server = _resource_server(cfg)
        verified = await server.verify_payment(payload, requirements[0])
    except Exception as exc:  # malformed header or facilitator error
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=402,
            content={"x402Version": 1, "error": f"invalid payment: {exc}"},
        )
    if not verified.is_valid:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=402,
            content={
                "x402Version": 1,
                "error": "payment verification failed",
                "invalidReason": verified.invalid_reason,
            },
        )

    payer = verified.payer or ident
    if not usage.allow_rate(payer):
        from fastapi.responses import JSONResponse

        collector.record_rate_limited()
        return JSONResponse(status_code=429, content={"error": "rate limit: 10 req/min"})

    request.state.pf_paid = True
    response = await call_next(request)

    try:
        settled = await server.settle_payment(payload, requirements[0])
        if not settled.success:
            # Facilitator settles fail intermittently (hiccups, pending windows).
            # Retrying is safe: EIP-3009 authorizations are single-use, so a
            # broadcast that actually landed cannot be replayed — this retry
            # only rescues settles that never broadcast.
            import asyncio as _asyncio

            await _asyncio.sleep(2.0)
            settled = await server.settle_payment(payload, requirements[0])
        response.headers[PAYMENT_RESPONSE_HEADER] = safe_base64_encode(
            settled.model_dump_json()
        )
        if settled.payer:
            response.headers["X-Preflight-Payer"] = settled.payer
        if settled.success:
            # Route price is the source of truth for revenue accounting:
            # facilitator settle responses may omit `amount` or report it
            # inconsistently. Our own ROUTE_PRICES is what the 402 charged.
            amount = _route_amount(route)
            collector.record_settle(route, amount, settled.payer)
            from preflight.notify.discord import notify_sale

            await notify_sale(route, amount, settled.payer, settled.transaction)
    except Exception as exc:  # served but settle failed — surfaced in header only
        response.headers["X-Preflight-Settle-Error"] = str(exc)[:200]

    return response


def validate_pay_to_address() -> bool:
    """Refuse to serve paid routes without a receiver in production."""
    cfg = config()
    if cfg.dev_mode:
        return True
    addr = cfg.pay_to_address
    if not addr:
        raise RuntimeError(
            "PREFLIGHT_PAY_TO_ADDRESS not configured and DEV_MODE is not enabled"
        )
    if not addr.startswith("0x") or len(addr) != 42:
        raise RuntimeError(f"Invalid PREFLIGHT_PAY_TO_ADDRESS format: {addr}")
    return True
