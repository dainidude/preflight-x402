"""UA classification + agent-only auto-trial tests (wave 7, omp/arbiter).

The HARD INVARIANTS: crawlers/browsers/unknown UAs always see the raw 402
(Bazaar/x402scan/registry validators depend on it); only recognised agent
SDKs auto-trial, sharing the same 10/day identity quota.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from preflight.api import app as app_mod  # noqa: E402
from preflight.api import payments as pay_mod  # noqa: E402
from preflight.api.app import app  # noqa: E402
from preflight.api.metrics import collector  # noqa: E402
from preflight.api.payments import DEMO_DAILY, classify_user_agent  # noqa: E402
from preflight.config import Config  # noqa: E402
from preflight.types import ScanReport  # noqa: E402


def _fake_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "_run_analysis", lambda s, cn=None, sv=None: ScanReport(
        verdict="looks_ok", score=0.0, findings=[], contract_name=cn,
        solc_version=sv or "0.8.20", scanner_versions={"slither": "0.11.6"},
        scanned_at="2026-11-16T00:00:00Z", source_sha256="c" * 64,
    ))


@pytest.fixture()
def trial_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("FREE_TIER", "1")
    monkeypatch.setenv("PREFLIGHT_PAY_TO_ADDRESS", "0x" + "11" * 20)
    Config.reload()


# ---------------------------------------------------------------------------
# classify_user_agent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ua,expected", [
    ("OpenAI/Python 1.55", "agent"),
    ("codex-cli/1.0", "agent"),
    ("claude-ai/1.0 (anthropic)", "agent"),
    ("langchain-agent/0.3", "agent"),
    ("Google-Extended; Gemini", "agent"),
    ("GoogleBot/2.1", "crawler"),
    ("x402scan-crawler/1.0", "crawler"),
    ("Better Uptime Bot", "crawler"),
    ("HeadlessChrome/130", "crawler"),
    ("Mozilla/5.0 (X11) AppleWebKit/537.36 Chrome/130.0 Safari/537.36", "browser"),
    ("Mozilla/5.0 Firefox/132.0", "browser"),
    ("python-httpx/2.27", "unknown"),
    ("curl/8.5.0", "unknown"),
    ("Wget/1.21", "unknown"),
    ("Go-http-client/2.0", "unknown"),
    ("", "unknown"),
    ("something-else/1.0", "unknown"),
])
def test_classify_user_agent(ua: str, expected: str) -> None:
    assert classify_user_agent(ua) == expected


def test_crawler_token_beats_agent_token() -> None:
    """Precedence: a UA claiming to be an agent AND a bot is a crawler."""
    assert classify_user_agent("openai-bot/1.0") == "crawler"


# ---------------------------------------------------------------------------
# Auto-trial invariants (middleware)
# ---------------------------------------------------------------------------

def _make_req(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pay_mod, "build_requirements", lambda route, cfg=None: _make_requirements()
    )


def _make_requirements() -> list[object]:
    from x402.schemas.payments import PaymentRequirements

    return [
        PaymentRequirements(
            scheme="exact", network="eip155:84532",
            asset="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            amount="10000", pay_to="0x" + "11" * 20, max_timeout_seconds=60,
            extra={"name": "USDC", "version": "2", "description": "scan"},
        )
    ]


def test_crawler_ua_gets_raw_402(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"User-Agent": "x402scan-crawler/1.0"},
        )
        assert r.status_code == 402
        assert r.headers.get("x-preflight-free-tier") is None


def test_unknown_http_client_gets_raw_402(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare httpx/curl (Bazaar probers) must never be auto-trialed."""
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"User-Agent": "python-httpx/2.27"},
        )
        assert r.status_code == 402
        assert r.headers.get("x-preflight-free-tier") is None


def test_browser_ua_gets_raw_402(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"User-Agent": "Mozilla/5.0 Chrome/130.0 Safari/537.36"},
        )
        assert r.status_code == 402


def test_agent_ua_auto_trials_without_header(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"User-Agent": "OpenAI/Python 1.55"},
        )
        assert r.status_code == 200
        assert r.headers.get("x-preflight-free-tier") == "granted"


def test_agent_auto_trial_shares_and_exhausts_quota(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        for _ in range(pay_mod.FREE_TIER_DAILY):
            r = c.post(
                "/v1/scan", json={"source": "contract A {}"},
                headers={"User-Agent": "OpenAI/Python 1.55"},
            )
            assert r.status_code == 200
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"User-Agent": "OpenAI/Python 1.55"},
        )
        assert r.status_code == 402


def test_explicit_header_still_grants_for_any_ua(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-home tests: TestClient's UA + explicit header = existing behavior."""
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    with TestClient(app) as c:
        r = c.post(
            "/v1/scan", json={"source": "contract A {}"},
            headers={"X-Preflight-Free": "trial", "User-Agent": "curl/8.5.0"},
        )
        assert r.status_code == 200
        assert r.headers.get("x-preflight-free-tier") == "granted"


def test_ua_402_counters_increment(
    trial_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_scan(monkeypatch)
    _make_req(monkeypatch)
    pay_mod.usage = pay_mod.UsageTracker()
    # Access the collector via the MODULE — other tests rebind
    # metrics.collector (test_api_complete.py:168) and the middleware
    # resolves the current binding per call; a direct import would go stale.
    from preflight.api import metrics as m_mod

    m_mod.collector._ua_402_agent = 0
    m_mod.collector._ua_402_crawler = 0
    m_mod.collector._ua_402_unknown = 0
    with TestClient(app) as c:
        c.post("/v1/scan", json={"source": "contract A {}"},
               headers={"User-Agent": "GoogleBot/2.1"})
        c.post("/v1/scan", json={"source": "contract A {}"},
               headers={"User-Agent": "python-httpx/2.27"})
        assert m_mod.collector._ua_402_crawler == 1
        assert m_mod.collector._ua_402_unknown == 1
        assert m_mod.collector._ua_402_agent == 0
        snap = m_mod.collector.snapshot()
        assert snap["ua_402_crawler"] == 1
        assert snap["ua_402_unknown"] == 1


def test_demo_daily_is_five() -> None:
    assert DEMO_DAILY == 5
