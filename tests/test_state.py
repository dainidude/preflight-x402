"""Durable state, dead letters, digest, restore — arbiter tests (v2)."""

from __future__ import annotations

import json
from pathlib import Path

from preflight.api.state import (
    DurableState,
    SettleDeadLetterStorage,
    format_digest,
)


def test_durable_state_roundtrip(tmp_path: Path) -> None:
    st = DurableState(str(tmp_path))
    st.save({"a": 1, "nested": {"b": [1, 2]}})
    assert DurableState(str(tmp_path)).load() == {"a": 1, "nested": {"b": [1, 2]}}


def test_durable_state_corrupt_tolerant(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{not json")
    assert DurableState(str(tmp_path)).load() is None


def test_durable_state_disabled_is_noop() -> None:
    st = DurableState(None)
    st.save({"a": 1})  # must not raise
    assert st.load() is None


def test_dead_letter_append_read_resolve(tmp_path: Path) -> None:
    dl = SettleDeadLetterStorage(str(tmp_path))
    dl.append_dead_letter({"route": "POST /v1/scan", "error": "boom"})
    dl.append_dead_letter({"route": "POST /v1/sim", "error": "boom2"})
    entries = dl.read_all_dead_letters()
    assert len(entries) == 2 and entries[0]["route"] == "POST /v1/scan"
    assert dl.mark_resolved(0) is True
    remaining = dl.read_all_dead_letters()
    assert len(remaining) == 1 and remaining[0]["route"] == "POST /v1/sim"
    resolved = [json.loads(l) for l in (tmp_path / "settle_deadletter.resolved.jsonl").read_text().splitlines()]
    assert resolved[0]["route"] == "POST /v1/scan"
    assert dl.mark_resolved(99) is False


def test_metrics_restore_from_disk(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in [k for k in sys.modules if k.startswith("preflight.api.metrics")]:
        del sys.modules[m]
    from preflight.api import metrics as m_mod

    m_mod.collector = m_mod.MetricsCollector()
    m_mod.collector.record_scan("POST /v1/scan", "block", True)
    m_mod.collector.record_settle("POST /v1/scan", 10000, "0xpayer")

    fresh = m_mod.MetricsCollector()  # same state dir → restores
    snap = fresh.snapshot()
    assert snap["scans_total"] == 1
    assert snap["revenue_base_units"] == 10000
    assert snap["unique_payers"] == 1
    monkeypatch.delenv("STATE_DIR")


def test_usage_tracker_restore(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in [k for k in sys.modules if k == "preflight.api.payments"]:
        del sys.modules[m]
    from preflight.api import payments as p_mod

    t = p_mod.UsageTracker()
    for _ in range(3):
        assert t.consume_free("agent-1") is True
    t._maybe_flush(force=True)
    fresh = p_mod.UsageTracker()
    left = fresh.free_left("agent-1")
    assert left == p_mod.FREE_TIER_DAILY - 3
    monkeypatch.delenv("STATE_DIR")


def test_format_digest_numbers() -> None:
    text = format_digest(
        {
            "uptime_s": 3600.5,
            "scans_total": 12,
            "scans_paid": 3,
            "scans_free": 9,
            "settlements_total": 3,
            "revenue_usdc": 0.16,
            "unique_payers": 2,
            "payment_required_total": {"POST /v1/scan": 7},
            "rate_limited_total": 1,
            "scans_by_verdict": {"block": 5, "looks_ok": 7},
        }
    )
    assert "Scans Total: 12" in text
    assert "$0.16" in text
    assert "Unique Payers: 2" in text
    assert "Payment Required: 7" in text
    assert "block" in text


def test_attribution_counters_and_masking(tmp_path: Path, monkeypatch) -> None:
    from preflight.api.metrics import MetricsCollector

    c = MetricsCollector()
    c.record_scan("POST /v1/scan", "block", True, agent="0xc5925C971f5628ba1E195C83Ce83317A70b3f30B")
    c.record_settle("POST /v1/scan", 10000, "0xc592…f30B", agent="0xc5925C971f5628ba1E195C83Ce83317A70b3f30B")
    c.record_scan("POST /v1/sim", "review", True, agent="0x5515f59a30fe2b9152cd5648a3a0d8c30c837d41")
    snap = c.snapshot()
    assert snap["agents_total"] == 2
    top = snap["top_agents"][0]
    assert top["agent"] == "0xc592…f30B" and top["revenue_base_units"] == 10000
    assert "…" in top["agent"]  # masked, never the full address
    # no agent -> not attributed
    c.record_scan("POST /v1/scan", "looks_ok", False)
    assert c.snapshot()["agents_total"] == 2


def test_attribution_persists(tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for m in [k for k in sys.modules if k.startswith("preflight.api.metrics")]:
        del sys.modules[m]
    from preflight.api import metrics as m_mod

    c = m_mod.MetricsCollector()
    c.record_settle("POST /v1/sim", 50000, None, agent="0xabc123456789def0")
    fresh = m_mod.MetricsCollector()
    snap = fresh.snapshot()
    assert snap["agents_total"] == 1
    assert snap["top_agents"][0]["revenue_base_units"] == 50000
    monkeypatch.delenv("STATE_DIR")
