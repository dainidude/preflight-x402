"""FastAPI application for Preflight — real core pipeline, real x402 payments.

Arbiter rebuild: the first cut never called the core pipeline and hardcoded
reports. This one runs the real analysis (Slither + custom detectors) in the
threadpool, signs reports (EIP-712), caches by source hash, and enforces the
x402 handshake via api.payments.x402_payment_middleware.
"""

from __future__ import annotations

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
)

app = FastAPI(title="Preflight Security API", version="0.1.0")
app.include_router(metrics_router)

_cache = LRUCache(maxsize=512, ttl=3600)

# Lazy core holder — tests monkeypatch this instead of the real pipeline.
_run_analysis: Any = None
_run_analysis_project: Any = None
_run_simulation: Any = None


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


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Preflight Security API",
        "version": "0.1.0",
        "description": "Pay-per-call smart-contract security scans. POST Solidity source, "
        "receive a severity-scored, EIP-712-attested verdict.",
        "prices": price_list(),
        "payment": {
            "protocol": "x402",
            "network": config().network,
            "asset": "USDC (test)",
            "facilitator": config().facilitator_url,
        },
        "endpoints": {
            "POST /v1/scan": "single-file scan",
            "POST /v1/scan/project": "multi-file project scan",
            "POST /v1/address-risk": "address risk heuristics",
            "GET /llms.txt": "LLM-facing service description",
            "GET /.well-known/x402.json": "x402 discovery metadata",
        },
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "time": _now()}


def _serve_public(path: str, media_type: str = "text/plain") -> Response:
    full = REPO_ROOT / "public" / path
    try:
        return Response(content=full.read_text(), media_type=media_type)
    except FileNotFoundError:
        return JSONResponse({"error": f"{path} not published yet"}, status_code=404)


@app.get("/llms.txt")
async def llms_txt() -> Response:
    return _serve_public("llms.txt")


@app.get("/.well-known/x402.json")
async def x402_well_known() -> Response:
    return _serve_public(".well-known/x402.json", media_type="application/json")


@app.get("/.well-known/x402")
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


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> Response:
    return _serve_public("favicon.svg", media_type="image/svg+xml")


@app.post("/v1/scan")
async def scan_source(request: ScanRequest, raw: Request) -> Response:
    """Scan Solidity source — $0.01 via x402."""
    sha = hashlib.sha256(request.source.encode()).hexdigest()
    cached = _cache.get(sha)
    if cached is not None:
        return JSONResponse(cached)

    report: ScanReport = await _call_core(
        _get_run_analysis(), request.source, request.contract_name, request.solc_version
    )
    return _finalize(report, route="POST /v1/scan", paid=getattr(raw.state, "pf_paid", False))


@app.post("/v1/scan/project")
async def scan_project(request: ProjectScanRequest, raw: Request) -> Response:
    """Scan a multi-file project — $0.10 via x402."""
    files_key = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(request.files.items())).encode()
    ).hexdigest()
    cached = _cache.get(files_key)
    if cached is not None:
        return JSONResponse(cached)

    report: ScanReport = await _call_core(
        _get_run_analysis_project(), request.files, request.contract_name, request.solc_version
    )
    return _finalize(report, route="POST /v1/scan/project", paid=getattr(raw.state, "pf_paid", False))


@app.post("/v1/sim")
async def sim_transaction(request: SimRequest, raw: Request) -> Response:
    """Transaction pre-flight simulation — $0.05 via x402."""
    report: SimReport = await _call_core(_get_run_simulation(), request)
    body = report.model_dump(mode="json")
    if body.get("attestation") is None:
        body["attestation"] = sign_report(body)
    from preflight.api.metrics import collector

    collector.record_scan("POST /v1/sim", report.verdict, getattr(raw.state, "pf_paid", False))
    return JSONResponse(body)


@app.post("/v1/address-risk")
def address_risk(request: AddressRiskRequest, raw: Request) -> Response:
    """Static address risk heuristics — $0.005 via x402."""
    findings: list[Finding] = []
    addr = request.address

    try:
        from web3 import Web3

        Web3.to_checksum_address(addr)
    except Exception:
        findings.append(
            Finding(
                id="invalid-checksum",
                title="Invalid address format",
                severity="low",
                confidence=1.0,
                description="Address is not a valid EIP-55 checksum address.",
                detector="heuristic:validation",
            )
        )

    verdict = "review" if any(f.severity == "medium" for f in findings) else "looks_ok"
    report = AddressRiskReport(
        verdict=verdict,
        address=addr,
        findings=findings,
        checked_at=_now(),
    )
    body = report.model_dump(mode="json")
    body["attestation"] = sign_report(body)
    from preflight.api.metrics import collector

    collector.record_scan("POST /v1/address-risk", verdict, getattr(raw.state, "pf_paid", False))
    return JSONResponse(body)


def _run_sync(func: Any, *args: Any) -> Any:
    """DEPRECATED sync path — kept for compatibility."""
    return func(*args)


async def _call_core(func: Any, *args: Any) -> ScanReport:
    """Call the core pipeline whether it is sync or async (hermes made it async)."""
    import inspect

    result = func(*args)
    if inspect.iscoroutine(result):
        result = await result
    return result


def _finalize(report: ScanReport, route: str = "POST /v1/scan", paid: bool = False) -> Response:
    body = report.model_dump(mode="json")
    if body.get("attestation") is None:
        body["attestation"] = sign_report(body)
    _cache.put(report.source_sha256, body)
    from preflight.api.metrics import collector

    collector.record_scan(route, report.verdict, paid)
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
    "/v1/scan/project": "0.10",
    "/v1/address-risk": "0.005",
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
        "looks_ok | review | block. First 10 scans/day are free with the "
        "X-Preflight-Free: trial header. Full service description at /llms.txt."
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
