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
from datetime import datetime, timezone
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
    "GET /favicon.ico",
    "GET /favicon.svg",
    "GET /.well-known/x402.json",
    "GET /.well-known/x402",
    "GET /robots.txt",
    "GET /v1/verify",
}

# route -> (price, description)
ROUTE_PRICES: dict[str, tuple[str, str]] = {
    "POST /v1/scan": ("$0.01", "Smart-contract security scan (static + custom DeFi detectors)"),
    "POST /v1/sim": ("$0.05", "Transaction pre-flight simulation (RPC eth_call)"),
    "POST /v1/sim/deep": ("$0.25", "Deep transaction simulation (anvil fork: gas, balance deltas, traces)"),
    "POST /v1/scan/project": ("$0.10", "Multi-file project security scan"),
    "POST /v1/address-risk": ("$0.005", "Address risk heuristics"),
    "POST /v1/token-risk": ("$0.05", "Token risk by address: honeypot verdict, transfer tax, owner privileges"),
    "POST /v1/bundle": ("$0.15", "Bundle: token-risk + address-risk + (optional) source scan in one call"),
}

FREE_TIER_DAILY = 10
RATE_LIMIT_PER_MIN = 10
PAID_DAILY_CAP_USD = 10  # AGENTS.md contract: $10/day per-payer approximation

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
        input={"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "data": "0x", "value_wei": "0"},
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
    "POST /v1/sim/deep": declare_discovery_extension(
        input={"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "data": "0x", "value_wei": "0"},
        input_schema={
            "properties": {
                "to": {"type": "string", "description": "Target contract address"},
                "data": {"type": "string", "description": "0x-hex calldata (omit for plain value transfer)"},
                "value_wei": {"type": "string", "description": "Wei value, decimal string"},
            },
            "required": ["to"],
        },
        body_type="json",
        output=OutputConfig(
            example={
                "verdict": "review",
                "reverted": False,
                "gas_used": "43281",
                "balance_delta_wei": "-10000000000001",
                "internal_calls": ["0xabc… → transfer(0xa9059cbb)"],
            }
        ),
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
    "POST /v1/token-risk": declare_discovery_extension(
        input={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
        input_schema={
            "properties": {"address": {"type": "string", "description": "Token contract address to analyze"}},
            "required": ["address"],
        },
        body_type="json",
        output=OutputConfig(
            example={
                "verdict": "review",
                "notes": ["Transfer tax: 3.5%"],
                "findings": [{"id": "token:transfer-tax", "severity": "medium"}],
            }
        ),
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

        if cfg.cdp_api_key_id and cfg.cdp_api_key_secret:
            # Coinbase CDP Facilitator: KYT/OFAC on every tx + Bazaar indexing
            # (a settled payment through CDP is what triggers discovery).
            from cdp.x402 import create_facilitator_config

            facilitator = HTTPFacilitatorClient(
                create_facilitator_config(
                    api_key_id=cfg.cdp_api_key_id,
                    api_key_secret=cfg.cdp_api_key_secret,
                )
            )
        else:
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
        _requirements_cache[route] = requirements
    return _requirements_cache[route]


def _bazaar_decl(route: str) -> dict[str, Any] | None:
    """The route's Bazaar declaration with the method injected.

    The CDP validator requires info.input.method; declare_discovery_extension
    omits it (normally enriched at runtime). All Preflight paid routes are POST.
    """
    bazaar = ROUTE_BAZAAR.get(route)
    if not bazaar:
        return None
    bazaar = json.loads(json.dumps(bazaar))
    bazaar.setdefault("bazaar", {}).setdefault("info", {}).setdefault("input", {})[
        "method"
    ] = "POST"
    return bazaar


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
    """Usage counters. Free-tier day counts persist to STATE_DIR when set
    (rate windows are ephemeral by design)."""

    def __init__(self) -> None:
        self._free_day: dict[str, tuple[str, int]] = {}
        self._paid_day: dict[str, tuple[str, float]] = {}
        self._window: dict[str, deque[float]] = defaultdict(deque)
        self._last_flush = 0.0
        self._usage_file = None
        import os

        d = os.getenv("STATE_DIR")
        if d:
            from pathlib import Path

            self._usage_file = Path(d) / "usage.json"
            self._restore_usage()

    def _restore_usage(self) -> None:
        import json

        if not self._usage_file or not self._usage_file.exists():
            return
        try:
            data = json.loads(self._usage_file.read_text())
            self._free_day = {k: tuple(v) for k, v in data.get("free", {}).items()}
        except (OSError, json.JSONDecodeError):
            pass

    def _maybe_flush(self, force: bool = False) -> None:
        import json
        import time

        if not self._usage_file:
            return
        if not force and time.monotonic() - self._last_flush < 2.0:
            return
        self._last_flush = time.monotonic()
        try:
            tmp = self._usage_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"free": {k: list(v) for k, v in self._free_day.items()}})
            )
            tmp.replace(self._usage_file)
        except OSError:
            pass

    @staticmethod
    def ip_identity(request: Request) -> str:
        """IP(+proxy-hint)+UA identity only — no x-payer: trial quotas can't be
        rotated by spoofing a payer header (grill finding)."""
        fwd = request.headers.get("x-forwarded-for", "")
        client = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
        ua = request.headers.get("user-agent", "")
        return hashlib.sha256(f"{client}|{ua}".encode()).hexdigest()[:20]

    @staticmethod
    def fingerprint(request: Request) -> str:
        payer = request.headers.get("x-payer")
        if payer:
            return payer.lower()
        # Behind proxies (Modal, Cloudflare) request.client.host is the proxy —
        # prefer the first X-Forwarded-For hop so per-client quotas stay real.
        fwd = request.headers.get("x-forwarded-for", "")
        client = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
        ua = request.headers.get("user-agent", "")
        return hashlib.sha256(f"{client}|{ua}".encode()).hexdigest()[:20]

    def free_left(self, ident: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day, used = self._free_day.get(ident, (today, 0))
        if day != today:
            return FREE_TIER_DAILY
        return max(0, FREE_TIER_DAILY - used)

    def consume_free(self, ident: str) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day, used = self._free_day.get(ident, (today, 0))
        if day != today:
            used = 0
        if used >= FREE_TIER_DAILY:
            return False
        self._free_day[ident] = (today, used + 1)
        self._maybe_flush()
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

    def paid_today_usd(self, ident: str) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day, usd = self._paid_day.get(ident, (today, 0.0))
        return usd if day == today else 0.0

    def record_paid_usd(self, ident: str, usd: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day, usd_sofar = self._paid_day.get(ident, (today, 0.0))
        if day != today:
            usd_sofar = 0.0
        self._paid_day[ident] = (today, usd_sofar + usd)
        self._maybe_flush()

    def allow_paid_today(self, ident: str, usd_requested: float) -> bool:
        return self.paid_today_usd(ident) + usd_requested <= PAID_DAILY_CAP_USD


usage = UsageTracker()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

def _payment_required_response(
    route: str,
    requirements: list[PaymentRequirements],
    request: Request,
) -> Response:
    """Spec-shaped x402 V2 402: body + PAYMENT-REQUIRED header both carry the
    full PaymentRequired (version 2, resource, accepts) with the route's
    Bazaar declaration attached."""
    from fastapi.responses import JSONResponse
    from x402.schemas.payments import PaymentRequired, ResourceInfo

    price, description = ROUTE_PRICES[route]
    resource = ResourceInfo(
        url=str(request.url),
        description=description,
        mime_type="application/json",
        service_name="Preflight",
        tags=["security", "solidity", "smart-contracts", "simulation"],
    )
    bazaar = _bazaar_decl(route)
    payment_required = PaymentRequired(
        error=f"Payment required for {route}",
        resource=resource,
        accepts=requirements,
        extensions=bazaar,
    )
    body = payment_required.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = safe_base64_encode(
        payment_required.model_dump_json(by_alias=True, exclude_none=True)
    )
    body = dict(body)
    body["hint"] = (
        "First 10 calls/day are free: retry with header 'X-Preflight-Free: trial'. "
        "Payment: USDC on Base via x402 — retry with the PAYMENT-SIGNATURE header."
    )
    return JSONResponse(
        status_code=402,
        content=body,
        headers={PAYMENT_REQUIRED_HEADER: encoded, "Cache-Control": "no-store"},
    )


async def x402_payment_middleware(request: Request, call_next: Callable) -> Response:
    from preflight.api.metrics import collector

    route = f"{request.method} {request.url.path}"
    if route in FREE_ROUTES or route not in ROUTE_PRICES:
        return await call_next(request)

    cfg = config()
    request.state.pf_paid = False
    # Stable per-client identity: attribution uses the same full value on the
    # scan and settle paths. x-payer is display metadata only — never trusted
    # for quotas (grill: rotating it yielded unlimited free trials).
    ident = usage.fingerprint(request)
    request.state.pf_agent = ident
    # Production sanity on EVERY paid request: Modal/ASGI never runs __main__,
    # so this is the only place a misconfigured pay_to gets caught (grill #4).
    if not cfg.dev_mode:
        validate_pay_to_address()

    # Optional enterprise API-key layer (default off). When keys are set, paid
    # routes require a valid X-Preflight-Key in addition to the x402 payment.
    if cfg.api_keys:
        provided = request.headers.get("x-preflight-key", "")
        if provided not in cfg.api_keys:
            from fastapi.responses import JSONResponse

            collector.record_401(route)
            return JSONResponse(
                status_code=401,
                content={"error": "invalid or missing X-Preflight-Key"},
                headers={"WWW-Authenticate": "X-Preflight-Key"},
            )

    if cfg.dev_mode:
        response = await call_next(request)
        response.headers["X-Preflight-Dev"] = "bypass"
        return response


    # Free tier is OPT-IN via explicit header so that x402 crawlers/validators
    # (which never send it) always see the required 402 payment challenge.
    # Trial quota keys on IP/UA identity ONLY (x-payer is not trusted here).
    free_trial = request.headers.get("x-preflight-free") == "trial"
    trial_ident = usage.ip_identity(request)
    if cfg.free_tier and free_trial and usage.consume_free(trial_ident):
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
        return _payment_required_response(route, requirements, request)

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
    price_usd = _route_amount(route) / 1_000_000
    if not usage.allow_paid_today(payer, price_usd):
        from fastapi.responses import JSONResponse

        collector.record_rate_limited()
        return JSONResponse(
            status_code=429,
            content={"error": f"daily cap reached: ${PAID_DAILY_CAP_USD}/day per payer"},
        )

    request.state.pf_paid = True
    response = await call_next(request)

    try:
        # Settle-resilience v2: 3 attempts (1s/3s backoff). Retrying is safe:
        # EIP-3009 authorizations are single-use, so a broadcast that actually
        # landed cannot be replayed — retries only rescue settles that never
        # broadcast. Final failure lands in the dead-letter journal for
        # scripts/settle_repair.py.
        import asyncio as _asyncio

        settled = await server.settle_payment(payload, requirements[0])
        for delay in (1.0, 3.0):
            if settled.success:
                break
            await _asyncio.sleep(delay)
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
            usage.record_paid_usd(settled.payer or ident, amount / 1_000_000)
            collector.record_settle(route, amount, settled.payer, agent=ident)
            from preflight.notify.discord import notify_sale

            await notify_sale(route, amount, settled.payer, settled.transaction)
        else:
            _dead_letter(route, payload, requirements[0], settled)
    except Exception as exc:  # served but settle failed — surfaced in header only
        response.headers["X-Preflight-Settle-Error"] = str(exc)[:200]
        try:
            _dead_letter(route, payload, requirements[0], error=str(exc)[:200])
        except Exception:
            pass

    return response


def _dead_letter(
    route: str,
    payload: Any,
    requirement: PaymentRequirements,
    settled: Any = None,
    error: str | None = None,
) -> None:
    """Journal an unsettled payment for replay (STATE_DIR required)."""
    import time as _time

    from preflight.config import config as _cfg

    cfg = _cfg()
    if not cfg.state_dir:
        return
    from preflight.api.state import get_settle_dead_letter_storage

    storage = get_settle_dead_letter_storage()
    if storage is None:
        from preflight.api.state import configure as _configure

        _configure(cfg.state_dir)
        storage = get_settle_dead_letter_storage()
    storage.append_dead_letter(
        {
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "route": route,
            "amount_base_units": _route_amount(route),
            "payer": getattr(settled, "payer", None),
            "transaction": getattr(settled, "transaction", None),
            "error": error
            or (None if settled is None else getattr(settled, "error_reason", None)),
            "requirements": requirement.model_dump(mode="json", by_alias=True),
            "payload": payload.model_dump(mode="json", by_alias=True) if hasattr(payload, "model_dump") else payload,
        }
    )


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
