"""Preflight MCP server.

Exposes three tools over MCP:

* ``preflight_check``  — POST the Solidity source to ``POST /v1/scan`` and
  return the resulting :class:`~preflight.types.ScanReport` JSON. Payments flow
  through x402 on the API side, so this tool is a thin HTTP client.
* ``preflight_sim``    — POST a transaction to ``POST /v1/sim`` and return the
  resulting :class:`~preflight.types.SimReport` JSON (revert, gas, findings).
* ``preflight_pricing`` — free ``GET /`` price list.

Run with stdio (default) or streamable HTTP (``--http``):

    uv run python -m preflight.mcp.server
    uv run python -m preflight.mcp.server --http --port 8000
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..config import config
from ..types import ScanReport, SimReport

_DEV_HEADER = "X-Preflight-Dev"
_DEV_HEADER_VALUE = "bypass"


def _dev_headers() -> dict[str, str]:
    """Return the dev-mode bypass header when DEV_MODE is set on this server."""
    if config().dev_mode:
        return {_DEV_HEADER: _DEV_HEADER_VALUE}
    return {}


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
        response = await client.post(url, json=payload, headers=_dev_headers())

    if response.status_code == 402:
        raise ToolError(
            "Payment required (402). Run the API with DEV_MODE=1 for local use, "
            "or use the paying demo client (client/pay_client.py)."
        )
    response.raise_for_status()

    data = response.json()
    return ScanReport.model_validate(data).model_dump(mode="json")


async def simulate_transaction(
    to: str,
    data: str | None = None,
    value_wei: str | None = None,
) -> dict[str, Any]:
    """POST ``/v1/sim`` and return the parsed SimReport dict.

    Raises:
        ToolError: On a 402 payment-required response or any HTTP error.
    """
    payload: dict[str, Any] = {"to": to}
    if data is not None:
        payload["data"] = data
    if value_wei is not None:
        payload["value_wei"] = value_wei

    url = f"{config().api_url.rstrip('/')}/v1/sim"
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(url, json=payload, headers=_dev_headers())

    if response.status_code == 402:
        raise ToolError(
            "Payment required (402) for POST /v1/sim ($0.05 USDC on Base Sepolia). "
            "Run the API with DEV_MODE=1 for local use, or run the paying demo client: "
            "`uv run python client/pay_client.py --route sim`."
        )
    response.raise_for_status()

    report = SimReport.model_validate(response.json()).model_dump(mode="json")
    return report


async def pricing() -> dict[str, Any]:
    """GET ``/`` and return the free price list."""
    url = f"{config().api_url.rstrip('/')}/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
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
            "call ($0.01 USDC on Base Sepolia) that flows through x402; in dev "
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
            "gas used, and severity-scored findings. This is a paid call ($0.05 "
            "USDC on Base Sepolia) that flows through x402; in dev mode "
            "(DEV_MODE=1) no payment is required."
        ),
    )
    async def preflight_sim(
        to: str,
        data: str | None = None,
        value_wei: str | None = None,
    ) -> str:
        report = await simulate_transaction(to, data, value_wei)
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
