"""Tests for the Preflight MCP server (offline, no network).

The MCP tools are thin HTTP clients over the API, so we exercise the tool
wiring and the HTTP-call behavior with mocks; no real network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from preflight.mcp import server as mcp_server
from preflight.mcp.server import create_server


@pytest.fixture(autouse=True)
def _clear_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests to the key-off path; key tests opt in explicitly."""
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("PREFLIGHT_API_KEYS", raising=False)

REPORT: dict[str, Any] = {
    "verdict": "block",
    "score": 92.5,
    "findings": [
        {
            "id": "reentrancy-no-eth",
            "title": "Reentrancy on withdraw()",
            "severity": "critical",
            "confidence": 0.98,
            "description": "External call before state update",
            "snippet": None,
            "detector": "slither",
            "reference": None,
        }
    ],
    "contract_name": "Vault",
    "solc_version": "0.8.20",
    "scanner_versions": {"slither": "0.10.4"},
    "scanned_at": "2026-11-10T00:00:00Z",
    "source_sha256": "abc123",
    "attestation": None,
    "notes": [],
}


SIM_REPORT: dict[str, Any] = {
    "verdict": "block",
    "score": 85.0,
    "findings": [
        {
            "id": "sim:revert",
            "title": "Simulated call reverts",
            "severity": "high",
            "confidence": 1.0,
            "description": "ERC20: transfer amount exceeds balance",
            "snippet": None,
            "detector": "heuristic:simulation",
            "reference": None,
        }
    ],
    "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "from_address": "0x000000000000000000000000000000000000dEaD",
    "value_wei": "0",
    "reverted": True,
    "revert_reason": "ERC20: transfer amount exceeds balance",
    "gas_used": "24312",
    "simulated_at": "2026-11-12T00:00:00Z",
    "simulator": "rpc-ethcall:v1",
    "attestation": None,
    "notes": [],
}


async def _text(result: Any) -> str:
    """Extract the first text block from a CallToolResult."""
    return result.content[0].text


async def test_preflight_check_tool_returns_report(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_scan(source: str, contract_name: str | None = None) -> dict[str, Any]:
        assert "contract" in source
        assert contract_name == "Vault"
        return REPORT

    monkeypatch.setattr(mcp_server, "scan_source", fake_scan)
    server = create_server()
    result = await server.call_tool(
        "preflight_check", {"source": "contract Vault {}", "contract_name": "Vault"}
    )
    data = json.loads(await _text(result))
    assert data["verdict"] == "block"
    assert data["findings"][0]["severity"] == "critical"


async def test_preflight_pricing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_pricing() -> dict[str, Any]:
        return {"service": "Preflight Security API", "prices": {"POST /v1/scan": "$0.01"}}

    monkeypatch.setattr(mcp_server, "pricing", fake_pricing)
    server = create_server()
    result = await server.call_tool("preflight_pricing", {})
    data = json.loads(await _text(result))
    assert data["prices"]["POST /v1/scan"] == "$0.01"


async def test_preflight_check_lists_as_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_scan(source: str, contract_name: str | None = None) -> dict[str, Any]:
        return REPORT

    monkeypatch.setattr(mcp_server, "scan_source", fake_scan)
    server = create_server()
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert {"preflight_check", "preflight_pricing"} <= names


async def test_preflight_sim_tool_returns_report(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sim(
        to: str,
        data: str | None = None,
        value_wei: str | None = None,
        mode: str = "quick",
    ) -> dict[str, Any]:
        assert to == SIM_REPORT["to"]
        assert data == "0xdeadbeef"
        assert value_wei == "0"
        assert mode == "deep"
        return SIM_REPORT

    monkeypatch.setattr(mcp_server, "simulate_transaction", fake_sim)
    server = create_server()
    result = await server.call_tool(
        "preflight_sim",
        {"to": SIM_REPORT["to"], "data": "0xdeadbeef", "value_wei": "0", "mode": "deep"},
    )
    data = json.loads(await _text(result))
    assert data["reverted"] is True
    assert data["revert_reason"] == "ERC20: transfer amount exceeds balance"
    assert data["findings"][0]["id"] == "sim:revert"


async def test_preflight_sim_lists_as_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sim(
        to: str,
        data: str | None = None,
        value_wei: str | None = None,
        mode: str = "quick",
    ) -> dict[str, Any]:
        return SIM_REPORT

    monkeypatch.setattr(mcp_server, "simulate_transaction", fake_sim)
    server = create_server()
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert {"preflight_check", "preflight_sim", "preflight_pricing"} <= names


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("http error", request=httpx.Request("POST", "http://x"), response=httpx.Response(self.status_code))


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_headers: dict[str, str] = {}
        self.last_json: Any = None
        self.last_url = ""

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.last_headers = headers or {}
        self.last_json = json
        self.last_url = url
        return self._response

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.last_headers = headers or {}
        self.last_url = url
        return self._response


def _cfg(dev_mode: bool) -> Any:
    return SimpleNamespace(dev_mode=dev_mode, api_url="http://127.0.0.1:8000")


async def test_scan_source_sends_dev_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsyncClient(_FakeResponse(200, REPORT))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=True))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    result = await mcp_server.scan_source("contract Vault {}", "Vault")
    assert result["verdict"] == "block"
    assert fake.last_headers.get("X-Preflight-Dev") == "bypass"
    assert fake.last_headers.get("Accept") == "application/json"
    assert "X-Preflight-Key" not in fake.last_headers


async def test_scan_source_sends_api_key_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(200, REPORT))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.setenv("MCP_API_KEY", "mcp-secret")
    monkeypatch.delenv("PREFLIGHT_API_KEYS", raising=False)

    await mcp_server.scan_source("contract Vault {}", "Vault")
    assert fake.last_headers.get("X-Preflight-Key") == "mcp-secret"
    assert fake.last_headers.get("Accept") == "application/json"


async def test_api_key_falls_back_to_first_preflight_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(200, REPORT))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("PREFLIGHT_API_KEYS", "first-key, second-key")

    await mcp_server.scan_source("contract Vault {}", "Vault")
    assert fake.last_headers.get("X-Preflight-Key") == "first-key"


async def test_api_key_401_is_actionable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(401, {"error": "unauthorized"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(mcp_server.ToolError, match="MCP_API_KEY"):
        await mcp_server.scan_source("contract Vault {}", "Vault")


async def test_pricing_negotiates_json_when_root_serves_html_to_star(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET / serves HTML to */* and JSON only when Accept says so."""
    seen: list[tuple[str | None, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        accept = request.headers.get("accept")
        seen.append((accept, dict(request.headers)))
        if accept == "application/json":
            return httpx.Response(
                200,
                json={"service": "Preflight", "prices": {"POST /v1/scan": "$0.01"}},
            )
        return httpx.Response(
            200,
            text="<html><body>Preflight landing</body></html>",
            headers={"content-type": "text/html"},
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_server.httpx,
        "AsyncClient",
        lambda *a, **k: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setenv("MCP_API_KEY", "pricing-secret")
    monkeypatch.delenv("PREFLIGHT_API_KEYS", raising=False)

    result = await mcp_server.pricing()
    assert result["service"] == "Preflight"
    assert result["prices"]["POST /v1/scan"] == "$0.01"
    assert seen[0][0] == "application/json"
    assert seen[0][1].get("x-preflight-key") == "pricing-secret"

    # The MCP tool itself also parses JSON (not the HTML landing page).
    tool_result = await create_server().call_tool("preflight_pricing", {})
    tool_data = json.loads(await _text(tool_result))
    assert tool_data["prices"]["POST /v1/scan"] == "$0.01"

    # Prove the other negotiation branch is real HTML for a non-JSON client.
    with httpx.Client(transport=httpx.MockTransport(handler)) as plain:
        html = plain.get("http://127.0.0.1:8000/")
    assert html.headers["content-type"].startswith("text/html")
    assert "landing" in html.text


async def test_scan_source_402_raises_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsyncClient(_FakeResponse(402, {"x402Version": 1, "error": "Payment required"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(mcp_server.ToolError):
        await mcp_server.scan_source("contract Vault {}", "Vault")


async def test_scan_source_validates_report(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed report from the API must not pass through unvalidated.
    fake = _FakeAsyncClient(_FakeResponse(200, {"not": "a report"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(Exception):
        await mcp_server.scan_source("contract Vault {}", "Vault")


async def test_simulate_transaction_sends_dev_header_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(200, SIM_REPORT))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=True))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    result = await mcp_server.simulate_transaction(SIM_REPORT["to"], "0xdeadbeef", "0")
    assert result["reverted"] is True
    assert fake.last_url.endswith("/v1/sim")
    assert fake.last_headers.get("X-Preflight-Dev") == "bypass"
    assert fake.last_headers.get("Accept") == "application/json"
    assert fake.last_json == {"to": SIM_REPORT["to"], "data": "0xdeadbeef", "value_wei": "0"}


async def test_simulate_transaction_deep_routes_to_sim_deep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep_report = dict(SIM_REPORT)
    deep_report["gas_used"] = "51234"
    deep_report["balance_delta_wei"] = "-1000000000000000"
    deep_report["internal_calls"] = [
        "0x833589…cf7e → transfer(address,uint256) 0xa9059cbb",
        "0x833589…cf7e → revert ERC20: transfer amount exceeds balance",
    ]
    deep_report["simulator"] = "anvil-fork:v1"
    fake = _FakeAsyncClient(_FakeResponse(200, deep_report))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    result = await mcp_server.simulate_transaction(SIM_REPORT["to"], mode="deep")
    assert fake.last_url.endswith("/v1/sim/deep")
    assert fake.last_json == {"to": SIM_REPORT["to"]}
    assert result["balance_delta_wei"] == "-1000000000000000"
    assert len(result["internal_calls"]) == 2


async def test_simulate_transaction_rejects_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(mcp_server.ToolError, match="quick.*deep"):
        await mcp_server.simulate_transaction(SIM_REPORT["to"], mode="turbo")


async def test_simulate_transaction_402_raises_actionable_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(402, {"error": "Payment required"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(mcp_server.ToolError) as excinfo:
        await mcp_server.simulate_transaction(SIM_REPORT["to"])
    assert "$0.05" in str(excinfo.value)
    assert "--route sim" in str(excinfo.value)


async def test_simulate_transaction_deep_402_names_deep_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsyncClient(_FakeResponse(402, {"error": "Payment required"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(mcp_server.ToolError) as excinfo:
        await mcp_server.simulate_transaction(SIM_REPORT["to"], mode="deep")
    assert "$0.25" in str(excinfo.value)
    assert "--route sim-deep" in str(excinfo.value)


async def test_simulate_transaction_validates_report(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsyncClient(_FakeResponse(200, {"not": "a sim report"}))
    monkeypatch.setattr(mcp_server, "config", lambda: _cfg(dev_mode=False))
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda *a, **k: fake)

    with pytest.raises(Exception):
        await mcp_server.simulate_transaction(SIM_REPORT["to"])


# ---------------------------------------------------------------------------
# Discovery files carry the real launch values
# ---------------------------------------------------------------------------


def _discovery() -> dict[str, Any]:
    root = Path(__file__).resolve().parent.parent / "public"
    return json.loads((root / ".well-known" / "x402.json").read_text(encoding="utf-8"))


def test_x402_discovery_real_values() -> None:
    doc = _discovery()
    payment = doc["payment"]
    assert payment["pay_to"] == "0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41"
    assert payment["network_caip"] == "eip155:8453"
    assert payment["network"] == "base"
    assert payment["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert payment["scheme"] == "exact"
    assert payment["facilitator"] == "https://x402.org/facilitator"

    assert doc["pricing"]["POST /v1/scan"]["price"] == "$0.01"
    assert doc["pricing"]["POST /v1/scan"]["amount"] == "10000"
    assert doc["pricing"]["POST /v1/sim"]["price"] == "$0.05"
    assert doc["pricing"]["POST /v1/sim"]["amount"] == "50000"
    assert doc["pricing"]["POST /v1/sim/deep"]["price"] == "$0.25"
    assert doc["pricing"]["POST /v1/sim/deep"]["amount"] == "250000"
    assert doc["pricing"]["POST /v1/sim/deep"]["network"] == "eip155:8453"
    assert (
        doc["pricing"]["POST /v1/sim/deep"]["asset"]
        == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    assert doc["pricing"]["POST /v1/scan/project"]["price"] == "$0.10"
    assert doc["pricing"]["POST /v1/scan/project"]["amount"] == "100000"
    assert doc["pricing"]["POST /v1/address-risk"]["price"] == "$0.005"
    assert doc["pricing"]["POST /v1/address-risk"]["amount"] == "5000"
    for route in doc["pricing"].values():
        assert route["network"] == "eip155:8453"
        assert route["asset"] == payment["asset"]
        assert route["scheme"] == "exact"
    assert "GET /metrics" in doc["endpoints"]


def test_x402_discovery_bazaar_extensions() -> None:
    bazaar = _discovery()["extensions"]["bazaar"]
    for route in (
        "POST /v1/scan",
        "POST /v1/sim",
        "POST /v1/sim/deep",
        "POST /v1/scan/project",
        "POST /v1/address-risk",
    ):
        extension = bazaar[route]
        assert extension["info"]["input"]["type"] == "http"
        assert extension["info"]["input"]["method"] == "POST"
        assert extension["info"]["input"]["bodyType"] == "json"
        assert "schema" in extension
        assert extension["info"]["output"]["type"] == "json"
    assert bazaar["mcp"]["preflight_sim"]["info"]["input"]["type"] == "mcp"
    assert bazaar["mcp"]["preflight_sim"]["info"]["input"]["toolName"] == "preflight_sim"


def test_public_files_have_no_testnet_references() -> None:
    root = Path(__file__).resolve().parent.parent / "public"
    for name in (".well-known/x402.json", "llms.txt"):
        text = (root / name).read_text(encoding="utf-8")
        for needle in ("0x036CbD", "84532", "base-sepolia", "Sepolia", "testnet"):
            assert needle not in text, f"{name} still references {needle}"


def test_llms_txt_documents_new_surface() -> None:
    root = Path(__file__).resolve().parent.parent / "public"
    text = (root / "llms.txt").read_text(encoding="utf-8")
    assert "/v1/sim" in text
    assert "/v1/sim/deep" in text
    assert "$0.25" in text
    assert "anvil" in text
    assert "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" in text
    assert "0x036CbD" not in text
    assert "84532" not in text
    assert "base-sepolia" not in text
    assert "/metrics" in text
    assert "PAYMENT-SIGNATURE" in text
    assert "PAYMENT-RESPONSE" in text
    assert "402" in text
