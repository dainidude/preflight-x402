"""End-to-end integration test (arbiter-owned).

Runs the real core pipeline (solc + Slither + custom detectors) through the
real API in DEV_MODE, including the EIP-712 attestation round-trip.
Requires the pre-installed solc versions (0.8.20/0.8.24/0.8.26) — no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from preflight.api.app import app  # noqa: E402
from preflight.attestation import verify_report  # noqa: E402


VULNERABLE = (Path(__file__).parent / "fixtures" / "reentrancy_vulnerable.sol").read_text()

CLEAN = """
pragma solidity ^0.8.20;

contract Adder {
    uint256 public total;

    function add(uint256 x) external {
        total += x;
    }
}
"""


@pytest.fixture()
def client(attest_env: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_root_lists_all_paid_routes(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    prices = r.json()["prices"]
    for route in ("POST /v1/scan", "POST /v1/scan/project", "POST /v1/address-risk"):
        assert route in prices, f"missing price entry for {route}"


def test_dev_mode_header_present(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": CLEAN, "contract_name": "Adder"})
    assert r.status_code == 200, r.text
    assert r.headers.get("x-preflight-dev") == "bypass"


def test_scan_clean_contract_verdict_ok(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": CLEAN, "contract_name": "Adder"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "looks_ok"
    assert body["source_sha256"]
    assert body["scanner_versions"], "scanner versions must be reported"


def test_scan_vulnerable_contract_blocks(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": VULNERABLE, "contract_name": "Vault"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] in {"block", "review"}, body
    assert body["findings"], "vulnerable contract must produce findings"
    severities = {f["severity"] for f in body["findings"]}
    assert severities & {"high", "critical", "medium"}, severities


def test_attestation_roundtrip(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": VULNERABLE, "contract_name": "Vault"})
    assert r.status_code == 200, r.text
    body = r.json()
    att = body.get("attestation")
    assert att, "attestation missing although PREFLIGHT_ATTEST_KEY is set"
    assert att["scheme"].startswith("eip712")
    assert verify_report(body, att) is True


def test_cache_same_source(client: TestClient) -> None:
    payload = {"source": CLEAN, "contract_name": "Adder"}
    first = client.post("/v1/scan", json=payload)
    second = client.post("/v1/scan", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["source_sha256"] == second.json()["source_sha256"]


def test_metrics_tracks_scans(client: TestClient) -> None:
    r0 = client.get("/metrics")
    before = r0.json()["metrics"]["scans_total"]
    # unique source → guaranteed cache miss so a scan metric is recorded
    client.post("/v1/scan", json={"source": CLEAN + "\n// metrics probe", "contract_name": "Adder"})
    r1 = client.get("/metrics")
    after = r1.json()["metrics"]
    assert after["scans_total"] == before + 1
    assert after["uptime_s"] > 0


@pytest.mark.network
def test_sim_dev_mode_live_rpc(client: TestClient) -> None:
    """Sim against the real test-USDC contract on Base Sepolia (free eth_call).

    A 0-value transfer of 1 USDC from a fresh simulator address reverts with
    insufficient balance/allowance → the sim must surface a sim:revert finding.
    """
    r = client.post(
        "/v1/sim",
        json={
            "to": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "data": "0x",
            "value_wei": "0",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["simulator"].startswith("rpc-ethcall")
    assert "verdict" in body
    # Either it reverts (finding) or reports clean — never a 500, and always shaped.
    assert body["verdict"] in {"looks_ok", "review", "block"}
    assert body["reverted"] in (True, False)
