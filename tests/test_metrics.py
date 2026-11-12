"""Tests for Preflight metrics."""

import pytest
from threading import Thread
from typing import Dict
from src.preflight.api.metrics import MetricsCollector, get_router
from fastapi import FastAPI

@pytest.fixture
def collector():
    """Fixture that provides a fresh MetricsCollector for each test."""
    # Create a new collector for each test to ensure isolation
    collector = MetricsCollector()
    yield collector
    # Clean up (not strictly necessary as collector will be garbage collected)
    collector = None

@pytest.fixture
def router():
    """Fixture that provides the metrics router."""
    return get_router()

@pytest.fixture
def test_app(router: FastAPI):
    """Fixture that creates a test FastAPI app with the metrics router."""
    app = FastAPI()
    app.include_router(router)
    return app

def test_counters_increment(collector: MetricsCollector):
    """Test that counters increment correctly."""
    # Test scan recording
    collector.record_scan("/v1/scan", "looks_ok", True)
    collector.record_scan("/v1/scan", "block", True)
    collector.record_scan("/v1/scan", "review", False)
    
    # Test settlement recording
    collector.record_settle("/v1/scan", 100000, "0x123")
    
    # Test free tier recording
    collector.record_free("/v1/scan")
    
    # Test rate limited recording
    collector.record_rate_limited()
    
    # Test 402 recording
    collector.record_402("/v1/scan")
    collector.record_402("/v1/scan/project")
    
    # Get snapshot and verify
    snapshot = collector.snapshot()
    
    assert snapshot["scans_total"] == 3
    assert snapshot["scans_by_verdict"]["looks_ok"] == 1
    assert snapshot["scans_by_verdict"]["block"] == 1
    assert snapshot["scans_by_verdict"]["review"] == 1
    assert snapshot["scans_paid"] == 2
    assert snapshot["scans_free"] == 1
    assert snapshot["settlements_total"] == 1
    assert snapshot["revenue_base_units"] == 100000
    assert snapshot["revenue_usdc"] == 0.1
    assert snapshot["unique_payers"] == 1
    assert snapshot["free_tier_grants"]["/v1/scan"] == 1
    assert snapshot["rate_limited_total"] == 1
    assert snapshot["payment_required_total"]["/v1/scan"] == 1
    assert snapshot["payment_required_total"]["/v1/scan/project"] == 1

def test_revenue_math(collector: MetricsCollector):
    """Test revenue calculation math."""
    # Settle different amounts
    collector.record_settle("/v1/scan", 10000, "0x123")
    collector.record_settle("/v1/scan/project", 20000, "0x123")
    collector.record_settle("/v1/address-risk", 30000, "0x456")
    
    snapshot = collector.snapshot()
    
    # Total base units: 10000 + 20000 + 30000 = 60000
    assert snapshot["revenue_base_units"] == 60000
    # Total USDC: 60000 / 1e6 = 0.06
    assert snapshot["revenue_usdc"] == 0.06
    # Unique payers: 2 (0x123, 0x456)
    assert snapshot["unique_payers"] == 2

def test_unique_payers_dedupes(collector: MetricsCollector):
    """Test that unique payers are correctly deduplicated."""
    # Settle with same payer multiple times
    collector.record_settle("/v1/scan", 10000, "0x123")
    collector.record_settle("/v1/scan/project", 20000, "0x123")
    collector.record_settle("/v1/address-risk", 30000, "0x456")
    
    snapshot = collector.snapshot()
    
    # Should only count unique payers once
    assert snapshot["unique_payers"] == 2

def test_metrics_endpoint(test_app):
    """Test that /metrics endpoint returns correct data."""
    from fastapi.testclient import TestClient
    
    client = TestClient(test_app)
    
    # Record some metrics first
    from src.preflight.api.metrics import collector
    collector.record_scan("/v1/scan", "looks_ok", True)
    collector.record_settle("/v1/scan", 15000, "0x123")
    
    # Call metrics endpoint
    response = client.get("/metrics")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["status"] == "ok"
    assert "metrics" in data
    assert data["metrics"]["scans_total"] == 1
    assert data["metrics"]["revenue_base_units"] == 15000
    assert data["metrics"]["unique_payers"] == 1

@pytest.mark.asyncio
async def test_thread_safety():
    """Test thread safety with concurrent access."""
    collector = MetricsCollector()
    
    # Define function to be run in threads
    def record_scans(thread_id: int):
        for i in range(10):
            collector.record_scan("/v1/scan", "looks_ok", True)
    
    # Create and start threads
    threads = []
    for thread_id in range(5):
        thread = Thread(target=record_scans, args=(thread_id,))
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Verify total scans (should be 50: 5 threads * 10 scans each)
    snapshot = collector.snapshot()
    assert snapshot["scans_total"] == 50

def test_snapshot_structure():
    """Test that snapshot has all required fields."""
    collector = MetricsCollector()
    
    # Add some data
    collector.record_scan("/v1/scan", "looks_ok", True)
    collector.record_settle("/v1/scan", 5000, "0x123")
    
    snapshot = collector.snapshot()
    
    # Required fields
    required_fields = [
        "uptime_s",
        "scans_total",
        "scans_by_verdict",
        "scans_paid",
        "scans_free",
        "settlements_total",
        "revenue_base_units",
        "revenue_usdc",
        "unique_payers",
        "free_tier_grants",
        "rate_limited_total",
        "payment_required_total"
    ]
    
    for field in required_fields:
        assert field in snapshot, f"Missing required field: {field}"
    
    # Verify field types
    assert isinstance(snapshot["uptime_s"], float)
    assert isinstance(snapshot["scans_total"], int)
    assert isinstance(snapshot["scans_by_verdict"], dict)
    assert isinstance(snapshot["scans_paid"], int)
    assert isinstance(snapshot["scans_free"], int)
    assert isinstance(snapshot["settlements_total"], int)
    assert isinstance(snapshot["revenue_base_units"], int)
    assert isinstance(snapshot["revenue_usdc"], float)
    assert isinstance(snapshot["unique_payers"], int)
    assert isinstance(snapshot["free_tier_grants"], dict)
    assert isinstance(snapshot["rate_limited_total"], int)
    assert isinstance(snapshot["payment_required_total"], dict)

def test_empty_snapshot():
    """Test that fresh collector has zero values."""
    collector = MetricsCollector()
    
    snapshot = collector.snapshot()
    
    assert snapshot["scans_total"] == 0
    assert snapshot["settlements_total"] == 0
    assert snapshot["revenue_base_units"] == 0
    assert snapshot["revenue_usdc"] == 0.0
    assert snapshot["unique_payers"] == 0
    assert snapshot["rate_limited_total"] == 0
    assert len(snapshot["payment_required_total"]) == 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
