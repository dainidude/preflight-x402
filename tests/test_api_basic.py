"""API smoke tests (DEV_MODE) — rewritten by the arbiter for the real stack."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from preflight.api import app as app_mod  # noqa: E402
from preflight.api.app import app  # noqa: E402
from preflight.types import Finding, ScanReport  # noqa: E402


def fake_report(source: str, verdict: str = "looks_ok") -> ScanReport:
    import hashlib
    from datetime import datetime, timezone

    return ScanReport(
        verdict=verdict,
        score=10.0 if verdict != "looks_ok" else 0.0,
        findings=(
            [
                Finding(
                    id="reentrancy-eth",
                    title="Reentrancy",
                    severity="high",
                    confidence=0.9,
                    description="bad",
                    detector="slither",
                )
            ]
            if verdict != "looks_ok"
            else []
        ),
        contract_name="X",
        solc_version="0.8.20",
        scanner_versions={"slither": "0.11.6"},
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
    )


@pytest.fixture()
def client(dev_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    calls: list[str] = []

    def fake_run(source: str, contract_name: str | None = None, solc_version: str | None = None) -> ScanReport:
        calls.append(source)
        return fake_report(source, "block" if "reentr" in source.lower() else "looks_ok")

    monkeypatch.setattr(app_mod, "_run_analysis", fake_run)
    monkeypatch.setattr(app_mod, "_run_analysis_project", lambda files, cn=None, sv=None: fake_report(" ".join(files.values())))
    with TestClient(app) as c:
        yield c
    assert calls == calls  # keep ref


def test_root_lists_paid_routes(client: TestClient) -> None:
    data = client.get("/").json()
    assert data["service"]
    for route in ("POST /v1/scan", "POST /v1/scan/project", "POST /v1/address-risk"):
        assert route in data["prices"]


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_scan_dev_mode_bypasses_payment(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": "contract A {}"})
    assert r.status_code == 200, r.text
    assert r.headers.get("x-preflight-dev") == "bypass"


def test_scan_returns_report_shape(client: TestClient) -> None:
    r = client.post("/v1/scan", json={"source": "contract Reentrancy {}", "contract_name": "X"})
    body = r.json()
    assert body["verdict"] == "block"
    assert body["findings"][0]["severity"] == "high"
    assert body["scanner_versions"]["slither"]
    assert len(body["source_sha256"]) == 64


def test_cache_prevents_second_analysis(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def counting_run(source: str, contract_name: str | None = None, solc_version: str | None = None) -> ScanReport:
        seen.append(source)
        return fake_report(source)

    monkeypatch.setattr(app_mod, "_run_analysis", counting_run)
    payload = {"source": "contract Cached {}"}
    assert client.post("/v1/scan", json=payload).status_code == 200
    assert client.post("/v1/scan", json=payload).status_code == 200
    assert len(seen) == 1


def test_project_scan_dev(client: TestClient) -> None:
    r = client.post(
        "/v1/scan/project",
        json={"files": {"A.sol": "contract A {}", "B.sol": "contract Reentr {}"}},
    )
    assert r.status_code == 200
    assert r.headers.get("x-preflight-dev") == "bypass"


def test_address_risk_dev(client: TestClient) -> None:
    r = client.post("/v1/address-risk", json={"address": "0x" + "00" * 20})
    assert r.status_code == 200
    assert r.json()["verdict"] in {"looks_ok", "review", "block"}
