"""Preflight MCP server.

Exposes four tools over MCP:

* ``preflight_check``  — POST the Solidity source to ``POST /v1/scan`` and
  return the resulting :class:`~preflight.types.ScanReport` JSON. Payments flow
  through x402 on the API side, so this tool is a thin HTTP client.
* ``preflight_sim``    — POST a transaction to ``POST /v1/sim`` (quick) or
  ``POST /v1/sim/deep`` (anvil fork) and return the resulting
  :class:`~preflight.types.SimReport` JSON (revert, gas, balance deltas,
  internal calls).
* ``preflight_token_risk`` — POST a token address to ``POST /v1/token-risk``
  and return the honeypot/tax/owner-privilege verdict as a ``ScanReport``.
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
        name="preflight_pricing",
        description="Return the Preflight service info and price list (free).",
    )
    async def preflight_pricing() -> str:
        info = await pricing()
        return json.dumps(info, indent=2)

    return server


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
