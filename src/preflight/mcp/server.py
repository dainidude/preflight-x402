"""Preflight MCP server.

Exposes six tools over MCP:

* ``preflight_check``  — POST the Solidity source to ``POST /v1/scan`` and
  return the resulting :class:`~preflight.types.ScanReport` JSON. Payments flow
  through x402 on the API side, so this tool is a thin HTTP client.
* ``preflight_sim``    — POST a transaction to ``POST /v1/sim`` (quick) or
  ``POST /v1/sim/deep`` (anvil fork) and return the resulting
  :class:`~preflight.types.SimReport` JSON (revert, gas, balance deltas,
  internal calls).
* ``preflight_token_risk`` — POST a token address to ``POST /v1/token-risk``
  and return the honeypot/tax/owner-privilege verdict as a ``ScanReport``.
* ``preflight_bundle`` — POST an address (+ optional source) to
  ``POST /v1/bundle`` for combined token/address/source analysis.
* ``preflight_verify`` — free ``GET /v1/verify?digest=0x…`` for a past verdict.
* ``preflight_pricing`` — free ``GET /`` price list.

Run with stdio (default) or streamable HTTP (``--http``):

    uv run python -m preflight.mcp.server
    uv run python -m preflight.mcp.server --http --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import config
from ..types import ScanReport, SimReport

_DEV_HEADER = "X-Preflight-Dev"
_DEV_HEADER_VALUE = "bypass"
_ACCEPT_HEADER = "Accept"
_ACCEPT_JSON = "application/json"
_API_KEY_HEADER = "X-Preflight-Key"


def _api_key() -> str | None:
    """Return the optional Preflight API key for this MCP server.

    ``MCP_API_KEY`` wins; otherwise the first entry in the server's
    comma-separated ``PREFLIGHT_API_KEYS`` is used. Empty/unset means the
    API-key layer is off and behavior is unchanged.
    """
    direct = os.getenv("MCP_API_KEY", "").strip()
    if direct:
        return direct
    for candidate in os.getenv("PREFLIGHT_API_KEYS", "").split(","):
        key = candidate.strip()
        if key:
            return key
    return None


def _api_headers() -> dict[str, str]:
    """Headers for every Preflight API call.

    ``Accept: application/json`` is required because ``GET /`` serves HTML to
    browsers/scrapers unless a client explicitly negotiates JSON. The optional
    API key and dev bypass are added when configured.
    """
    headers = {_ACCEPT_HEADER: _ACCEPT_JSON}
    if config().dev_mode:
        headers[_DEV_HEADER] = _DEV_HEADER_VALUE
    key = _api_key()
    if key:
        headers[_API_KEY_HEADER] = key
    return headers


async def scan_source(source: str, contract_name: str | None = None) -> dict[str, Any]:
    """POST ``/v1/scan`` and return the parsed ScanReport dict.

    Raises:
        ToolError: On a 402 payment-required response or any HTTP error.
    """
    payload: dict[str, Any] = {"source": source}
    if contract_name:
        payload["contract_name"] = contract_name

    url = f"{config().api_url.rstrip('/')}/v1/scan"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload, headers=_api_headers())

    if response.status_code == 402:
        raise ToolError(
            "Payment required (402). Run the API with DEV_MODE=1 for local use, "
            "or use the paying demo client (client/pay_client.py)."
        )
    if response.status_code in (401, 403):
        raise ToolError(
            "API key required or rejected. Set MCP_API_KEY (or share one key from "
            "PREFLIGHT_API_KEYS) to the value configured on the API."
        )
    response.raise_for_status()

    data = response.json()
    return ScanReport.model_validate(data).model_dump(mode="json")


async def simulate_transaction(
    to: str,
    data: str | None = None,
    value_wei: str | None = None,
    mode: str = "quick",
) -> dict[str, Any]:
    """POST ``/v1/sim`` (quick) or ``/v1/sim/deep`` and return the SimReport dict.

    Args:
        to: Target contract address.
        data: Optional 0x-hex calldata.
        value_wei: Optional wei value sent (decimal string).
        mode: ``quick`` (default, $0.05) or ``deep`` (anvil fork, $0.25).

    Raises:
        ToolError: On an invalid mode, a 402 payment-required response, or any
            HTTP error.
    """
    if mode not in {"quick", "deep"}:
        raise ToolError("mode must be 'quick' or 'deep'")
    path = "/v1/sim/deep" if mode == "deep" else "/v1/sim"
    price = "$0.25" if mode == "deep" else "$0.05"

    payload: dict[str, Any] = {"to": to}
    if data is not None:
        payload["data"] = data
    if value_wei is not None:
        payload["value_wei"] = value_wei

    url = f"{config().api_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(url, json=payload, headers=_api_headers())

    if response.status_code == 402:
        route_flag = "sim-deep" if mode == "deep" else "sim"
        raise ToolError(
            f"Payment required (402) for POST {path} ({price} USDC on Base mainnet). "
            "Run the API with DEV_MODE=1 for local use, or run the paying demo client: "
            f"`uv run python client/pay_client.py --route {route_flag}`."
        )
    if response.status_code in (401, 403):
        raise ToolError(
            "API key required or rejected. Set MCP_API_KEY (or share one key from "
            "PREFLIGHT_API_KEYS) to the value configured on the API."
        )
    response.raise_for_status()

    report = SimReport.model_validate(response.json()).model_dump(mode="json")
    return report


async def token_risk(address: str) -> dict[str, Any]:
    """POST ``/v1/token-risk`` and return the parsed ScanReport dict.

    Args:
        address: Token contract address (0x-prefixed, 20 bytes).

    Raises:
        ToolError: On a 402 payment-required response, a rejected API key, or
            any HTTP error.
    """
    payload: dict[str, Any] = {"address": address}
    url = f"{config().api_url.rstrip('/')}/v1/token-risk"
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(url, json=payload, headers=_api_headers())

    if response.status_code == 402:
        raise ToolError(
            "Payment required (402) for POST /v1/token-risk ($0.05 USDC on Base "
            "mainnet). Run the API with DEV_MODE=1 for local use, or run the paying "
            "demo client: `uv run python client/pay_client.py --route token-risk "
            "--address 0x...`."
        )
    if response.status_code in (401, 403):
        raise ToolError(
            "API key required or rejected. Set MCP_API_KEY (or share one key from "
            "PREFLIGHT_API_KEYS) to the value configured on the API."
        )
    response.raise_for_status()

    report = ScanReport.model_validate(response.json()).model_dump(mode="json")
    return report


async def bundle_analysis(address: str, source: str | None = None) -> dict[str, Any]:
    """POST ``/v1/bundle`` and return the combined analysis JSON.

    Args:
        address: Token or contract address to analyze.
        source: Optional Solidity source for the source-scan component.

    Raises:
        ToolError: On a 402 payment-required response, a rejected API key, or
            any HTTP error.
    """
    payload: dict[str, Any] = {"address": address}
    if source is not None:
        payload["source"] = source

    url = f"{config().api_url.rstrip('/')}/v1/bundle"
    async with httpx.AsyncClient(timeout=240.0) as client:
        response = await client.post(url, json=payload, headers=_api_headers())

    if response.status_code == 402:
        raise ToolError(
            "Payment required (402) for POST /v1/bundle ($0.15 USDC on Base "
            "mainnet). Run the API with DEV_MODE=1 for local use, or run the paying "
            "demo client: `uv run python client/pay_client.py --route bundle "
            "--address 0x...`."
        )
    if response.status_code in (401, 403):
        raise ToolError(
            "API key required or rejected. Set MCP_API_KEY (or share one key from "
            "PREFLIGHT_API_KEYS) to the value configured on the API."
        )
    response.raise_for_status()
    return response.json()


async def verify_digest(digest: str, attestation: str | None = None) -> dict[str, Any]:
    """GET ``/v1/verify?digest=...`` and return the verification result.

    This is a free discovery/verification route; no payment header is needed.
    ``attestation`` is optional here for clients that can supply the signed
    attestation JSON; the API accepts it as a second query parameter.
    """
    url = f"{config().api_url.rstrip('/')}/v1/verify?digest={quote(digest, safe='')}"
    if attestation is not None:
        url += f"&attestation={quote(attestation, safe='')}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=_api_headers())
    response.raise_for_status()
    return response.json()


async def pricing() -> dict[str, Any]:
    """GET ``/`` and return the free price list.

    Sends ``Accept: application/json`` because ``/`` serves the HTML landing
    page to browsers/scrapers and JSON only when explicitly negotiated.
    """
    url = f"{config().api_url.rstrip('/')}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=_api_headers())
    response.raise_for_status()
    return response.json()


def create_server() -> MCPServer:
    """Build the Preflight MCP server with its tools registered."""
    server = MCPServer(
        name="preflight",
        title="Preflight",
        description=(
            "Pay-per-call smart-contract & transaction pre-flight security API. "
            "Submit Solidity source or a transaction and receive a severity-scored, "
            "signed security verdict, paid via x402."
        ),
        version="0.1.0",
    )

    @server.tool(
        name="preflight_check",
        description=(
            "Scan Solidity source and return a security verdict. This is a paid "
            "call ($0.01 USDC on Base mainnet) that flows through x402; in dev "
            "mode (DEV_MODE=1) no payment is required."
        ),
    )
    async def preflight_check(source: str, contract_name: str | None = None) -> str:
        report = await scan_source(source, contract_name)
        return json.dumps(report, indent=2)

    @server.tool(
        name="preflight_sim",
        description=(
            "Simulate a transaction against a contract and return revert reason, "
            "gas used, balance deltas, and severity-scored findings. "
            "mode='quick' costs $0.05 (eth_call); mode='deep' costs $0.25 "
            "(anvil local fork with gas, balance_delta_wei, and internal_calls). "
            "Paid in USDC on Base mainnet via x402; DEV_MODE=1 skips payment."
        ),
    )
    async def preflight_sim(
        to: str,
        data: str | None = None,
        value_wei: str | None = None,
        mode: str = "quick",
    ) -> str:
        report = await simulate_transaction(to, data, value_wei, mode)
        return json.dumps(report, indent=2)

    @server.tool(
        name="preflight_token_risk",
        description=(
            "Check a token contract before trading it. Returns a honeypot "
            "transfer verdict, transfer tax percentage, and owner privileges as "
            "a severity-scored ScanReport. Paid call ($0.05 USDC on Base mainnet) "
            "via x402; DEV_MODE=1 skips payment."
        ),
    )
    async def preflight_token_risk(address: str) -> str:
        report = await token_risk(address)
        return json.dumps(report, indent=2)

    @server.tool(
        name="preflight_bundle",
        description=(
            "Run a combined token-risk, address-risk, and optional source scan "
            "for one address in a single paid call ($0.15 USDC on Base mainnet). "
            "Returns the bundle JSON."
        ),
    )
    async def preflight_bundle(address: str, source: str | None = None) -> str:
        result = await bundle_analysis(address, source)
        return json.dumps(result, indent=2)

    @server.tool(
        name="preflight_verify",
        description=(
            "Verify a past Preflight attestation by source digest (free). "
            "Returns the verification result JSON."
        ),
    )
    async def preflight_verify(digest: str, attestation: str | None = None) -> str:
        result = await verify_digest(digest, attestation)
        return json.dumps(result, indent=2)

    @server.tool(
        name="preflight_pricing",
        description="Return the Preflight service info and price list (free).",
    )
    async def preflight_pricing() -> str:
        info = await pricing()
        return json.dumps(info, indent=2)

    return server


def create_http_app():
    """ASGI app for hosted deployments.

    Layers (outermost first): CORS → surface middleware → the MCP streamable app.

    - CORS: the SDK's ``allowed_origins`` supports EXACT matches only — a
      ``"*"`` entry is cargo-cult that 403s every cross-origin browser client
      (Glama Inspector, web agents). Real CORS is handled here instead, and the
      SDK's DNS-rebinding origin check is disabled (Modal's edge already routes
      by Host; POST Content-Type and body-size checks still apply inside the SDK).
    - Surface middleware: serves ``/``, ``/robots.txt`` and the Smithery server
      card, and intercepts bare ``GET /mcp`` probes (no session + no SSE Accept)
      with a friendly JSON card instead of the SDK's opaque 400 "Missing session
      ID" — the only real "error" Modal's dashboard showed. Legitimate MCP
      traffic (POST requests, SSE GETs with a session id) passes through untouched.
    - The SDK app stays at ROOT with its own lifespan (Mounts do not propagate
      lifespan — the session-manager task group would never start).
    """
    from starlette.datastructures import Headers
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mcp.server.transport_security import TransportSecuritySettings

    server = create_server()
    mcp_app = server.streamable_http_app(
        stateless_http=True,  # tools are stateless API calls; no server-side session
        # state → directory health checkers that cache session IDs never hit
        # "Session not found" (was the Glama unhealthy flag); clients that send
        # session headers simply don't need them.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,  # wildcard origins unsupported by the SDK; CORS below
        ),
    )

    async def _send_json(payload: dict, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse(payload)(scope, receive, send)

    async def _send_text(text: str, scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse(text)(scope, receive, send)

    class _SurfaceMiddleware:
        """Extra routes + friendly probe handling around the MCP app."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            path = scope.get("path", "")
            method = scope.get("method", "")
            if method in ("GET", "HEAD"):
                if path == "/" or path == "":
                    await _send_json(_root_info(), scope, receive, send)
                    return
                if path == "/robots.txt":
                    await _send_text(
                        "User-agent: *\nAllow: /\nContent-Signal: search=yes, ai-train=no, ai-input=yes\n",
                        scope, receive, send,
                    )
                    return
                if path == "/.well-known/mcp/server-card.json":
                    await _send_json(_server_card(), scope, receive, send)
                    return
                if path == "/mcp":
                    headers = Headers(scope=scope)
                    is_stream_get = (
                        headers.get("mcp-session-id")
                        and "text/event-stream" in headers.get("accept", "")
                    )
                    if not is_stream_get:
                        # Bare probe (browser, scanner, directory): friendly card
                        # instead of the SDK's 400 "Missing session ID".
                        await _send_json(_probe_card(), scope, receive, send)
                        return
                    # The hosted app is stateless_http=True: there is no
                    # server-side SSE session to attach to. Passing this GET to
                    # the SDK opens an infinite stream (state checkers/tests
                    # hang); return an explicit protocol error instead.
                    await JSONResponse(
                        {
                            "error": (
                                "SSE GET is not supported on this stateless "
                                "streamable-HTTP endpoint; POST JSON-RPC to /mcp."
                            )
                        },
                        status_code=400,
                    )(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    return CORSMiddleware(
        _SurfaceMiddleware(mcp_app),
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE", "HEAD"],
        allow_headers=["*"],
        expose_headers=["*"],
    )


def _root_info() -> dict:
    return {
        "service": "Preflight MCP",
        "description": "Pay-per-call smart-contract & token security for AI agents, over MCP.",
        "mcp_endpoint": "https://preflight--preflight-api-mcp.modal.run/mcp",
        "transport": "streamable-http",
        "tools": [
            "preflight_check",
            "preflight_sim",
            "preflight_token_risk",
            "preflight_bundle",
            "preflight_verify",
            "preflight_pricing",
        ],
        "api_origin": "https://preflight--preflight-api-api.us-east.modal.run",
        "pricing": (
            "scan $0.01 · sim $0.05 · deep sim $0.25 · token-risk $0.05 · "
            "bundle $0.15 · project $0.10 · address-risk $0.005 · verify free "
            "(USDC on Base via x402; first 10 calls/day free with "
            "X-Preflight-Free: trial)"
        ),
        "server_card": "/.well-known/mcp/server-card.json",
        "source": "https://github.com/dainidude/preflight-x402",
    }


def _probe_card() -> dict:
    info = _root_info()
    info["hint"] = "This is an MCP streamable-HTTP endpoint. MCP clients: POST JSON-RPC to this URL. Browsers see this info card."
    return info


def _server_card() -> dict:
    return {
        "serverInfo": {
            "name": "Preflight",
            "version": "0.1.0",
            "title": "Preflight — smart-contract & token security",
        },
        "authentication": None,  # open server; tools pay per call via x402
        "tools": [
            {
                "name": "preflight_check",
                "description": "Scan Solidity source and return a severity-scored, EIP-712-attested verdict ($0.01, 10 free/day).",
            },
            {
                "name": "preflight_sim",
                "description": "Simulate a transaction: revert decoding (quick, $0.05) or anvil-fork gas/balance/traces (deep, $0.25).",
            },
            {
                "name": "preflight_token_risk",
                "description": "Token risk by address: honeypot verdict, transfer tax %, owner privileges ($0.05).",
            },
            {
                "name": "preflight_pricing",
                "description": "Free: service info and price list.",
            },
        ],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight MCP server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable HTTP instead of stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port (default 8000).")
    parser.add_argument("--path", default="/mcp", help="Streamable HTTP path (default /mcp).")
    return parser.parse_args()


def main() -> None:
    """Entry point for ``python -m preflight.mcp.server``."""
    args = _parse_args()
    server = create_server()
    if args.http:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
