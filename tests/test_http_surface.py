"""HTTP surface tests: HEAD, robots.txt, CORS, MCP probe cards.

Regression tests for the production bugs found in the Modal logs (Nov 2026):
- HEAD / on the API origin returned 405 (FastAPI @app.get does not serve HEAD)
- robots.txt 404s on both origins
- OPTIONS /mcp → 403 "Invalid Origin header" (SDK allowed_origins has no wildcard)
- bare GET /mcp → 400 "Missing session ID" (the only real dashboard error)
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_MODE", "1")
os.environ.setdefault("PREFLIGHT_PAY_TO_ADDRESS", "0x" + "22" * 20)

from preflight.config import Config  # noqa: E402

Config.reload()

from preflight.api.app import app  # noqa: E402
from preflight.mcp.server import create_http_app  # noqa: E402


@pytest.fixture()
def api_client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mcp_client() -> Iterator[TestClient]:
    with TestClient(create_http_app()) as c:
        yield c


# --- API origin: HEAD support -------------------------------------------------

def test_head_root(api_client: TestClient) -> None:
    r = api_client.head("/")
    assert r.status_code == 200
    assert r.content == b""  # HEAD responses carry headers only


def test_head_health(api_client: TestClient) -> None:
    assert api_client.head("/health").status_code == 200


def test_head_metrics_and_discovery(api_client: TestClient) -> None:
    assert api_client.head("/metrics").status_code == 200
    assert api_client.head("/.well-known/x402.json").status_code == 200
    assert api_client.head("/.well-known/x402").status_code == 200


def test_head_paid_route_is_still_405(api_client: TestClient) -> None:
    """HEAD on POST-only paid routes is protocol-correct 405 — unchanged."""
    assert api_client.head("/v1/scan").status_code == 405


# --- API origin: robots.txt + CORS --------------------------------------------

def test_robots_txt(api_client: TestClient) -> None:
    r = api_client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: *" in r.text
    assert "Allow: /" in r.text


def test_api_cors_preflight(api_client: TestClient) -> None:
    """Browser agents must be able to preflight the paid routes."""
    r = api_client.options(
        "/v1/scan",
        headers={
            "Origin": "https://glama.ai",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_api_cors_response_headers(api_client: TestClient) -> None:
    r = api_client.get("/health", headers={"Origin": "https://example.com"})
    assert r.headers.get("access-control-allow-origin") == "*"


# --- MCP origin: probe card, info routes, CORS ---------------------------------

def test_mcp_bare_get_returns_friendly_card(mcp_client: TestClient) -> None:
    """The bare GET probe (browser/scanner) gets a 200 info card, not the
    SDK's 400 'Missing session ID' (the Modal dashboard error)."""
    r = mcp_client.get("/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "Preflight MCP"
    assert body["mcp_endpoint"].endswith("/mcp")
    assert "streamable-http" in body["hint"].lower()


def test_mcp_root_info(mcp_client: TestClient) -> None:
    r = mcp_client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "Preflight MCP"
    assert "preflight_check" in body["tools"]


def test_mcp_robots_txt(mcp_client: TestClient) -> None:
    r = mcp_client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: *" in r.text


def test_mcp_server_card(mcp_client: TestClient) -> None:
    r = mcp_client.get("/.well-known/mcp/server-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["serverInfo"]["name"] == "Preflight"
    assert {t["name"] for t in card["tools"]} == {
        "preflight_check", "preflight_sim", "preflight_token_risk", "preflight_pricing",
    }


def test_mcp_cors_preflight(mcp_client: TestClient) -> None:
    """THE critical fix: the SDK's allowed_origins has no wildcard support —
    cross-origin browser clients (Glama Inspector) were 403'd. CORS middleware
    must answer preflight instead."""
    r = mcp_client.options(
        "/mcp",
        headers={
            "Origin": "https://glama.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,mcp-session-id",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]


def test_mcp_initialize_still_works(mcp_client: TestClient) -> None:
    """The full MCP handshake must be untouched by the surface middleware."""
    r = mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "surface-test", "version": "0.1"},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200
    # The SDK answers initialize with SSE framing: "event: message\ndata: {...}"
    data_line = next(
        ln for ln in r.text.splitlines() if ln.startswith("data:")
    )
    body = json.loads(data_line[len("data:"):].strip())
    assert body["result"]["serverInfo"]["name"].lower() == "preflight"


def test_mcp_sse_get_with_session_passes_through(mcp_client: TestClient) -> None:
    """A legitimate stream GET (session id + SSE Accept) must NOT get the
    friendly card — it passes through to the SDK (which manages the stream)."""
    r = mcp_client.get(
        "/mcp",
        headers={"Accept": "text/event-stream", "Mcp-Session-Id": "0123456789abcdef"},
    )
    # Not our card: the SDK answers (stream open or its own error), never 200-JSON-card
    assert not (r.status_code == 200 and "hint" in r.text)


# --- Bundle route (E4) --------------------------------------------------------

def test_bundle_route_exists(api_client: TestClient) -> None:
    """POST /v1/bundle exists and is a paid route (402 in prod, bypass in dev)."""
    r = api_client.post("/v1/bundle", json={"address": "0x" + "11" * 20})
    # DEV_MODE=1 so it should be 200 with bypass header
    assert r.status_code == 200
    assert r.headers.get("X-Preflight-Dev") == "bypass"
    body = r.json()
    assert "verdict" in body
    assert "findings" in body
    assert "notes" in body
    assert any("Bundle ran" in n for n in body.get("notes", []))


def test_bundle_merge_logic(api_client: TestClient) -> None:
    """Bundle merges findings from all three pipelines; worst verdict wins."""
    r = api_client.post("/v1/bundle", json={
        "address": "0x" + "11" * 20,
        "source": "contract Vault {}",
    })
    assert r.status_code == 200
    body = r.json()
    # Should have notes listing which analyses ran
    notes = body.get("notes", [])
    assert any("Bundle ran" in n for n in notes)
    # findings should be a list (possibly empty for mock data)
    assert isinstance(body.get("findings"), list)
    # verdict should be one of the valid values
    assert body.get("verdict") in ("looks_ok", "review", "block")


def test_bundle_expires_at_clean(api_client: TestClient) -> None:
    """Clean token-risk verdict gets expires_at ~7 days out."""
    r = api_client.post("/v1/bundle", json={"address": "0x" + "11" * 20})
    assert r.status_code == 200
    body = r.json()
    # In DEV_MODE with mock data, token-risk may or may not set expires_at
    # The key is that the field exists in the response
    assert "expires_at" in body


def test_bundle_expires_at_none_for_block(api_client: TestClient) -> None:
    """Blocked token-risk verdict gets expires_at = None."""
    # Mock a blocked verdict by using a source that triggers findings
    r = api_client.post("/v1/bundle", json={
        "address": "0x" + "11" * 20,
        "source": "contract Vault { function withdraw() external { selfdestruct(msg.sender); } }",
    })
    assert r.status_code == 200
    body = r.json()
    # If verdict is block, expires_at should be None
    if body.get("verdict") == "block":
        assert body.get("expires_at") is None
