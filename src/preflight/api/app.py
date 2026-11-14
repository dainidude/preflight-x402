"""FastAPI application for Preflight — real core pipeline, real x402 payments.

Arbiter rebuild: the first cut never called the core pipeline and hardcoded
reports. This one runs the real analysis (Slither + custom detectors) in the
threadpool, signs reports (EIP-712), caches by source hash, and enforces the
x402 handshake via api.payments.x402_payment_middleware.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from preflight.api.cache import LRUCache
from preflight.api.metrics import router as metrics_router
from preflight.api.payments import price_list, validate_pay_to_address, x402_payment_middleware
from preflight.attestation import sign_report
from preflight.config import REPO_ROOT, config
from preflight.types import (
    AddressRiskReport,
    AddressRiskRequest,
    Finding,
    ProjectScanRequest,
    ScanReport,
    ScanRequest,
    SimReport,
    SimRequest,
    BundleRequest,
    TokenRiskRequest,
)

app = FastAPI(title="Preflight Security API", version="0.1.0")

# CORS: browser-based paying agents (wallet-in-browser x402 flows) need this;
# the API is keyless and stateless — CORS opens nothing that isn't public.
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app.include_router(metrics_router)
from preflight.api.discovery import router as discovery_router  # noqa: E402

app.include_router(discovery_router)

_cache = LRUCache(maxsize=512, ttl=3600)

# Lazy core holder — tests monkeypatch this instead of the real pipeline.
_run_analysis: Any = None
_run_analysis_project: Any = None
_run_simulation: Any = None
_token_risk: Any = None


def _get_run_analysis() -> Any:
    global _run_analysis
    if _run_analysis is None:
        from preflight.core.pipeline import run_analysis

        _run_analysis = run_analysis
    return _run_analysis


def _get_run_analysis_project() -> Any:
    global _run_analysis_project
    if _run_analysis_project is None:
        from preflight.core.project import run_analysis_project

        _run_analysis_project = run_analysis_project
    return _run_analysis_project


def _get_token_risk() -> Any:
    global _token_risk
    if _token_risk is None:
        from preflight.core.token_risk import run_token_risk

        _token_risk = run_token_risk
    return _token_risk


def _get_run_simulation() -> Any:
    global _run_simulation
    if _run_simulation is None:
        from preflight.core.simulation import run_simulation

        _run_simulation = run_simulation
    return _run_simulation


@app.middleware("http")
async def payment_gate(request: Request, call_next: Any) -> Response:
    return await x402_payment_middleware(request, call_next)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request) -> Response:
    """Content-negotiated root: JSON for API clients (Accept: application/json),
    HTML landing (with OG/meta tags) for browsers, scrapers and `*/*` clients —
    directory scrapers like x402scan read the description from the HTML."""
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse(
            {
                "service": "Preflight Security API",
                "version": "0.1.0",
                "description": "Pay-per-call smart-contract security scans. POST Solidity source, "
                "receive a severity-scored, EIP-712-attested verdict.",
                "prices": price_list(),
                "payment": {
                    "protocol": "x402",
                    "network": config().network,
                    "asset": "USDC",
                    "facilitator": config().facilitator_url,
                },
                "endpoints": {
                    "POST /v1/scan": "single-file scan",
                    "POST /v1/sim": "transaction simulation",
                    "POST /v1/sim/deep": "deep simulation (anvil fork)",
                    "POST /v1/scan/project": "multi-file project scan",
                    "POST /v1/address-risk": "address risk heuristics",
                    "GET /llms.txt": "LLM-facing service description",
                    "GET /.well-known/x402.json": "x402 discovery metadata",
                    "GET /metrics": "funnel/revenue metrics",
                },
            }
        )
    return _serve_public("landing.html", media_type="text/html")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health() -> dict[str, str]:
    return {"status": "ok", "time": _now()}


def _serve_public(path: str, media_type: str = "text/plain") -> Response:
    full = REPO_ROOT / "public" / path
    try:
        return Response(content=full.read_text(), media_type=media_type)
    except FileNotFoundError:
        return JSONResponse({"error": f"{path} not published yet"}, status_code=404)


@app.api_route("/llms.txt", methods=["GET", "HEAD"])
async def llms_txt() -> Response:
    return _serve_public("llms.txt")


# /.well-known/x402.json is served by the GENERATED discovery router
# (api/discovery.py) — prices/payment fields derive from code, no drift.


@app.api_route("/.well-known/x402", methods=["GET", "HEAD"])
async def x402_fanout(request: Request) -> dict[str, Any]:
    """x402scan compatibility fan-out (docs/DISCOVERY.md option B)."""
    base = str(request.base_url).rstrip("/")
    return {
        "version": 1,
        "resources": [
            f"{base}/v1/scan",
            f"{base}/v1/sim",
            f"{base}/v1/scan/project",
            f"{base}/v1/address-risk",
        ],
        "ownershipProofs": [config().pay_to_address] if config().pay_to_address else [],
    }


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
async def favicon() -> Response:
    return _serve_public("favicon.svg", media_type="image/svg+xml")


@app.api_route("/v1/verify", methods=["GET"], include_in_schema=False)
async def verify(digest: str = "", attestation: str = "") -> Response:
    """Free: verify a Preflight attestation against a source digest.

    Query params: digest (the source_sha256 from a report) + attestation (the
    attestation JSON from that report). Returns the verification result.
    """
    import json as _json

    from preflight.attestation import verify_report

    if not digest or not attestation:
        return JSONResponse(
            {"error": "digest and attestation params are required"}, status_code=422
        )
    try:
        att = _json.loads(attestation)
    except _json.JSONDecodeError:
        return JSONResponse({"error": "attestation must be valid JSON"}, status_code=422)
    report = {"source_sha256": digest, "verdict": att.get("message", {}).get("verdict", "")}
    valid = verify_report(report, att)
    expires_at = att.get("message", {}).get("expires_at")
    stale_note = None
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                stale_note = "stale — re-verify"
        except (ValueError, TypeError):
            pass
    result = {
        "valid": valid,
        "signer": att.get("signer"),
        "verdict": att.get("message", {}).get("verdict"),
        "source_sha256": digest,
        "checked_at": _now(),
    }
    if stale_note:
        result["stale"] = True
        result["note"] = stale_note
    if not valid:
        result["reason"] = "signature does not match the digest, or the report shape is invalid"
    return JSONResponse(result)


@app.api_route("/verdicts", methods=["GET", "HEAD"])
async def verdicts(raw: Request) -> Response:
    """Free: the latest trending-token verdicts (public social proof).

    Content-negotiated: JSON for agents (Accept: application/json),
    a minimal HTML card list for browsers and search engines.
    """
    import json as _json
    from pathlib import Path

    accept = raw.headers.get("accept", "")
    verdicts: list[dict] = []
    try:
        vf = Path("/data/state/verdicts.json")
        if vf.exists():
            verdicts = _json.loads(vf.read_text())
    except Exception:
        verdicts = []

    if "application/json" in accept:
        return JSONResponse({"verdicts": verdicts, "total": len(verdicts)})

    # HTML for browsers/crawlers — the SEO surface
    emoji = {"looks_ok": "✅", "review": "⚠️", "block": "🚨"}
    cards = []
    for v in verdicts[:30]:
        e = emoji.get(v["verdict"], "❓")
        cards.append(
            f'<div style="padding:12px 16px;border-bottom:1px solid #2a2820;">'
            f'<span style="font-size:1.4em">{e}</span> '
            f'<strong style="color:#FFB000">{v["symbol"]}</strong> '
            f'<span style="color:#8a8578">({v["address"][:6]}…)</span> — '
            f'<span style="color:{"#00c853" if v["verdict"]=="looks_ok" else "#ffb300" if v["verdict"]=="review" else "#ff5252"}">{v["verdict"]}</span>'
            + (f' — {v["top_finding"]}' if v.get("top_finding") else "")
            + f'<br><small style="color:#8a8578">{v["scanned_at"]}</small></div>'
        )
    body = (
        "<!DOCTYPE html><html><head><title>Preflight — latest Base token verdicts</title>"
        '<meta name="description" content="Daily attested security verdicts for trending Base tokens: honeypot checks, transfer tax, owner privileges. Pay-per-call via x402.">'
        "<style>body{font-family:monospace;background:#0A0A08;color:#d8d4c8;max-width:720px;margin:0 auto;padding:2rem}"
        "h1{color:#FFB000}a{color:#FFB000}</style></head><body>"
        "<h1>Preflight verdicts</h1>"
        "<p>Attested security checks on trending Base tokens. "
        '<a href="/llms.txt">Check any token for $0.05</a> — no signup, pay per call in USDC.</p>'
        + "".join(cards)
        + '<p style="margin-top:2rem"><a href="https://preflight--preflight-api-api.us-east.modal.run">Try Preflight →</a></p>'
        "</body></html>"
    )
    return Response(content=body, media_type="text/html")


@app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def robots() -> Response:
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nContent-Signal: search=yes, ai-train=no, ai-input=yes\n"
    )


@app.post("/v1/scan")
async def scan_source(request: ScanRequest, raw: Request) -> Response:
    """Scan Solidity source — $0.01 via x402."""
    sha = hashlib.sha256(
        f"{request.contract_name}\x00{request.solc_version}\x00{request.source}".encode()
    ).hexdigest()
    cached = _cache.get(sha)
    if cached is not None:
        return JSONResponse(cached)

    report: ScanReport = await _call_core(
        _get_run_analysis(), request.source, request.contract_name, request.solc_version
    )
    return _finalize(
        report,
        route="POST /v1/scan",
        paid=getattr(raw.state, "pf_paid", False),
        cache_key=sha,
        agent=getattr(raw.state, "pf_agent", None),
    )


@app.post("/v1/scan/project")
async def scan_project(request: ProjectScanRequest, raw: Request) -> Response:
    """Scan a multi-file project — $0.10 via x402."""
    files_key = hashlib.sha256(
        (
            f"{request.contract_name}\x00{request.solc_version}\x00"
            + "\n".join(f"{k}:{v}" for k, v in sorted(request.files.items()))
        ).encode()
    ).hexdigest()
    cached = _cache.get(files_key)
    if cached is not None:
        return JSONResponse(cached)

    report: ScanReport = await _call_core(
        _get_run_analysis_project(), request.files, request.contract_name, request.solc_version
    )
    return _finalize(
        report,
        route="POST /v1/scan/project",
        paid=getattr(raw.state, "pf_paid", False),
        cache_key=files_key,
        agent=getattr(raw.state, "pf_agent", None),
    )


@app.post("/v1/sim")
async def sim_transaction(request: SimRequest, raw: Request) -> Response:
    """Transaction pre-flight simulation — $0.05 via x402."""
    report: SimReport = await _call_core(_get_run_simulation(), request)
    return _finalize_sim(report, "POST /v1/sim", raw)


@app.post("/v1/token-risk")
async def token_risk(request: TokenRiskRequest, raw: Request) -> Response:
    """Token risk by address — honeypot, tax, owner privileges. $0.05 via x402."""
    try:
        report: ScanReport = await _call_core(_get_token_risk(), request.address)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return _finalize(
        report,
        route="POST /v1/token-risk",
        paid=getattr(raw.state, "pf_paid", False),
        agent=getattr(raw.state, "pf_agent", None),
        cache_key=f"token-risk:{request.address.lower()}",
    )




@app.post("/v1/bundle")
async def bundle(request: BundleRequest, raw: Request) -> Response:
    """Bundle: token-risk + address-risk + (optional) source scan. $0.15 via x402."""
    address = request.address
    notes: list[str] = []
    findings: list[Finding] = []
    scanner_versions: dict[str, str] = {}
    source_sha256 = hashlib.sha256(address.encode()).hexdigest()

    async def _token_risk():
        try:
            return await _call_core(_get_token_risk(), address)
        except ValueError:
            return None

    async def _address_risk():
        try:
            from web3 import Web3
            Web3.to_checksum_address(address)
        except Exception:
            return None
        rpc = config().rpc_url
        if not rpc:
            return None
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            code = w3.eth.get_code(Web3.to_checksum_address(address)) or b""
            if not code:
                return None
            code_hex = code.hex()
            addr_findings: list[Finding] = []
            if "363d3d373d3d3d363d73" in code_hex or "3d3d3d3d" in code_hex[:200]:
                addr_findings.append(Finding(
                    id="address:minimal-proxy", title="Minimal proxy (EIP-1167-style clone)",
                    severity="low", confidence=0.7,
                    description="Bytecode looks like a minimal proxy/clone; the logic lives at another address.",
                    detector="heuristic:bytecode",
                ))
            if "715018a6" in code_hex:
                addr_findings.append(Finding(
                    id="address:selfdestruct-selector", title="Self-destruct selector present in bytecode",
                    severity="medium", confidence=0.5,
                    description="A kill/selfdestruct selector is reachable; verify it is owner-gated.",
                    detector="heuristic:bytecode",
                ))
            return addr_findings
        except Exception:
            return None

    async def _source_scan():
        if not request.source:
            return None
        try:
            return await _call_core(
                _get_run_analysis(), request.source, request.contract_name, request.solc_version
            )
        except Exception:
            return None

    token_report, addr_findings, source_report = await asyncio.gather(
        _token_risk(), _address_risk(), _source_scan()
    )

    ran: list[str] = []
    if token_report is not None:
        ran.append("token-risk")
        findings.extend(token_report.findings)
        scanner_versions.update(token_report.scanner_versions)
        source_sha256 = token_report.source_sha256
    if addr_findings is not None:
        ran.append("address-risk")
        findings.extend(addr_findings)
    if source_report is not None:
        ran.append("source-scan")
        findings.extend(source_report.findings)
        scanner_versions.update(source_report.scanner_versions)
        source_sha256 = source_report.source_sha256

    notes.append(f"Bundle ran: {', '.join(ran)}")

    severities = {f.severity for f in findings}
    if severities & {"critical", "high"}:
        verdict = "block"
    elif "medium" in severities:
        verdict = "review"
    else:
        verdict = "looks_ok"

    expires_at: str | None = None
    if token_report is not None and token_report.verdict == "looks_ok":
        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")

    report = ScanReport(
        verdict=verdict,
        score=max([r.score for r in [token_report, source_report] if r is not None] + [0.0]),
        findings=findings,
        contract_name=request.contract_name,
        solc_version=request.solc_version,
        scanner_versions=scanner_versions,
        scanned_at=_now(),
        source_sha256=source_sha256,
        notes=notes,
        expires_at=expires_at,
    )
    return _finalize(
        report,
        route="POST /v1/bundle",
        paid=getattr(raw.state, "pf_paid", False),
        agent=getattr(raw.state, "pf_agent", None),
    )

@app.post("/v1/sim/deep")
async def sim_deep(request: SimRequest, raw: Request) -> Response:
    """Deep transaction simulation (anvil fork) — $0.25 via x402."""
    request.mode = "deep"
    try:
        report: SimReport = await _call_core(_get_run_simulation(), request)
    except NotImplementedError:
        return JSONResponse(
            {"error": "deep simulation tier is rolling out — try POST /v1/sim (quick)"},
            status_code=503,
        )
    return _finalize_sim(report, "POST /v1/sim/deep", raw)


def _finalize_sim(report: SimReport, route: str, raw: Request) -> Response:
    body = report.model_dump(mode="json")
    if body.get("attestation") is None:
        body["attestation"] = sign_report(body)
    from preflight.api.metrics import collector

    collector.record_scan(
        route, report.verdict, getattr(raw.state, "pf_paid", False),
        agent=getattr(raw.state, "pf_agent", None),
    )
    return JSONResponse(body)


@app.post("/v1/address-risk")
def address_risk(request: AddressRiskRequest, raw: Request) -> Response:
    """Address risk — checksum, contract-vs-EOA, basic bytecode flags. $0.005 via x402.

    P1.2 honesty upgrade: a valid checksum alone is not a product. When RPC_URL
    is configured, we check whether the address is a contract, whether it is a
    minimal proxy, and note selfdestruct reachability — without ever claiming
    more than was checked.
    """
    findings: list[Finding] = []
    notes: list[str] = []
    addr = request.address

    try:
        from web3 import Web3

        Web3.to_checksum_address(addr)
    except Exception:
        findings.append(
            Finding(
                id="invalid-checksum",
                title="Invalid address format",
                severity="medium",
                confidence=1.0,
                description="Address is not a valid EIP-55 checksum address — no on-chain checks were possible.",
                detector="heuristic:validation",
            )
        )
    else:
        rpc = config().rpc_url
        if rpc:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc))
                code = w3.eth.get_code(Web3.to_checksum_address(addr)) or b""
                if code:
                    notes.append(f"Contract ({len(code)} bytes of deployed bytecode)")
                    code_hex = code.hex()
                    if "363d3d373d3d3d363d73" in code_hex or "3d3d3d3d" in code_hex[:200]:
                        findings.append(
                            Finding(
                                id="address:minimal-proxy",
                                title="Minimal proxy (EIP-1167-style clone)",
                                severity="low",
                                confidence=0.7,
                                description="Bytecode looks like a minimal proxy/clone; the logic lives at another address — check the implementation.",
                                detector="heuristic:bytecode",
                            )
                        )
                    if "715018a6" in code_hex:
                        findings.append(
                            Finding(
                                id="address:selfdestruct-selector",
                                title="Self-destruct selector present in bytecode",
                                severity="medium",
                                confidence=0.5,
                                description="A kill/selfdestruct selector is reachable in the bytecode; verify it is owner-gated before trusting this contract.",
                                detector="heuristic:bytecode",
                            )
                        )
                else:
                    notes.append("EOA — no deployed code at this address")
            except Exception as exc:
                notes.append(f"On-chain checks unavailable (RPC error: {str(exc)[:80]})")
        else:
            notes.append("On-chain checks skipped: RPC_URL not configured")

    # Same severity rule as core scoring: critical/high → block, medium → review.
    severities = {f.severity for f in findings}
    if severities & {"critical", "high"}:
        verdict = "block"
    elif "medium" in severities:
        verdict = "review"
    else:
        verdict = "looks_ok"
    report = AddressRiskReport(
        verdict=verdict,
        address=addr,
        findings=findings,
        checked_at=_now(),
    )
    body = report.model_dump(mode="json")
    if notes:
        body["notes"] = notes
    body["attestation"] = sign_report(body)
    from preflight.api.metrics import collector

    collector.record_scan(
        "POST /v1/address-risk", verdict, getattr(raw.state, "pf_paid", False),
        agent=getattr(raw.state, "pf_agent", None),
    )
    return JSONResponse(body)


def _run_sync(func: Any, *args: Any) -> Any:
    """DEPRECATED sync path — kept for compatibility."""
    return func(*args)


async def _call_core(func: Any, *args: Any) -> ScanReport:
    """Call the core pipeline without blocking the event loop.

    Sync (CPU-bound) callables run in a worker thread — a slow Slither scan
    must never stall /health or the heartbeat (IMPROVEMENT_SPEC P0.1). Async
    callables are awaited directly.
    """
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(func):
        return await func(*args)
    return await asyncio.to_thread(func, *args)


def _finalize(
    report: ScanReport,
    route: str = "POST /v1/scan",
    paid: bool = False,
    cache_key: str | None = None,
    agent: str | None = None,
) -> Response:
    body = report.model_dump(mode="json")
    if body.get("attestation") is None:
        body["attestation"] = sign_report(body)
    _cache.put(cache_key or report.source_sha256, body)
    from preflight.api.metrics import collector

    collector.record_scan(route, report.verdict, paid, agent=agent)
    return JSONResponse(body)


if __name__ == "__main__":
    import os

    import uvicorn

    validate_pay_to_address()
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PREFLIGHT_PORT", "8000")))


# ---------------------------------------------------------------------------
# x402scan OpenAPI-first discovery (docs/DISCOVERY.md option A): enrich the
# auto-generated /openapi.json with per-operation x-payment-info + a declared
# 402 response, and top-level ownership proofs.
# ---------------------------------------------------------------------------

_PAID_PATHS: dict[str, str] = {  # path -> USD amount
    "/v1/scan": "0.01",
    "/v1/sim": "0.05",
    "/v1/sim/deep": "0.25",
    "/v1/scan/project": "0.10",
    "/v1/address-risk": "0.005",
    "/v1/token-risk": "0.05",
    "/v1/bundle": "0.15",
}


def _openapi_with_x402() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = _original_openapi()
    pay_to = config().pay_to_address
    if pay_to:
        schema.setdefault("x-discovery", {})["ownershipProofs"] = [pay_to]
    schema["info"]["contact"] = {
        "name": "Preflight",
        "email": "preflight-contact@protonmail.com",
        "url": "https://github.com/dainidude/preflight-x402",
    }
    schema["info"]["x-guidance"] = (
        "Paid smart-contract security API. POST Solidity source to /v1/scan "
        "($0.01), a transaction to /v1/sim ($0.05), multi-file projects to "
        "/v1/scan/project ($0.10), or an address to /v1/address-risk "
        "($0.005). Each POST returns HTTP 402 with payment requirements; pay "
        "via the x402 protocol (USDC, Base) and retry with the PAYMENT-SIGNATURE "
        "header to receive a severity-scored, EIP-712-attested verdict: "
        "looks_ok | review | block. Trading agents: POST a token address to "
        "/v1/token-risk ($0.05) for a honeypot verdict, transfer tax %, and owner-privilege "
        "checks. First 10 calls/day are free with the X-Preflight-Free: trial header. "
        "Full service description at /llms.txt."
    )

    for path, methods in schema.get("paths", {}).items():
        amount = _PAID_PATHS.get(path)
        for method, op in methods.items():
            if method.lower() not in {"post", "get", "put", "patch", "delete"}:
                continue
            if amount:
                # x402scan's zod schema wants protocols as an array of records.
                op["x-payment-info"] = {
                    "protocols": [{"x402": {}}],
                    "price": {"mode": "fixed", "currency": "USD", "amount": amount},
                }
                op.setdefault("responses", {})["402"] = {
                    "description": "x402 Payment Required (PAYMENT-REQUIRED header + accepts)",
                }
            else:
                op["security"] = []  # explicitly public (kills auth-mode warnings)

    app.openapi_schema = schema
    return schema


_original_openapi = app.openapi
app.openapi = _openapi_with_x402  # type: ignore[method-assign]
