"""Thread-safe funnel metrics for Preflight API."""

import threading
import time
from typing import Dict, Any

# Global start time for uptime calculation
_START_TIME = time.time()

class MetricsCollector:
    """Thread-safe metrics collector for Preflight API."""
    
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Initialize all counters
        self._scans_total = 0
        self._scans_by_verdict: Dict[str, int] = {}
        self._scans_paid = 0
        self._scans_free = 0
        self._settlements_total = 0
        self._revenue_base_units = 0
        self._unique_payers: Dict[str, bool] = {}
        self._free_tier_grants: Dict[str, int] = {}
        self._rate_limited_total = 0
        self._payment_required_total: Dict[str, int] = {}
    
    def record_scan(self, route: str, verdict: str, paid: bool) -> None:
        """Record a scan operation.
        
        Args:
            route: The API route that was scanned
            verdict: The scan verdict (looks_ok, review, block)
            paid: Whether payment was made for this scan
        """
        with self._lock:
            self._scans_total += 1
            self._scans_by_verdict[verdict] = self._scans_by_verdict.get(verdict, 0) + 1
            if paid:
                self._scans_paid += 1
            else:
                self._scans_free += 1
    
    def record_settle(self, route: str, amount_base_units: int, payer: str | None) -> None:
        """Record a payment settlement.
        
        Args:
            route: The API route that was paid for
            amount_base_units: Amount paid in base units (USDC * 1e6)
            payer: Payer address (can be None for free tier)
        """
        with self._lock:
            self._settlements_total += 1
            self._revenue_base_units += amount_base_units
            if payer:
                self._unique_payers[payer] = True
    
    def record_free(self, route: str) -> None:
        """Record a free-tier grant (per-route counter; scan counts come from record_scan).

        Args:
            route: The API route that was served free of charge
        """
        with self._lock:
            self._free_tier_grants[route] = self._free_tier_grants.get(route, 0) + 1
    
    def record_rate_limited(self) -> None:
        """Record a rate limited request."""
        with self._lock:
            self._rate_limited_total += 1
    
    def record_402(self, route: str) -> None:
        """Record a 402 payment required response.
        
        Args:
            route: The API route that returned 402
        """
        with self._lock:
            self._payment_required_total[route] = self._payment_required_total.get(route, 0) + 1
    
    def snapshot(self) -> Dict[str, Any]:
        """Get current metrics snapshot.
        
        Returns:
            Dictionary with all metrics fields
        """
        with self._lock:
            # Calculate uptime
            uptime_s = time.time() - _START_TIME
            
            return {
                "uptime_s": uptime_s,
                "scans_total": self._scans_total,
                "scans_by_verdict": self._scans_by_verdict.copy(),
                "scans_paid": self._scans_paid,
                "scans_free": self._scans_free,
                "settlements_total": self._settlements_total,
                "revenue_base_units": self._revenue_base_units,
                "revenue_usdc": round(self._revenue_base_units / 1_000_000, 2),
                "unique_payers": len(self._unique_payers),
                "free_tier_grants": self._free_tier_grants.copy(),
                "rate_limited_total": self._rate_limited_total,
                "payment_required_total": self._payment_required_total.copy(),
            }

# Module-level singleton
collector = MetricsCollector()

# FastAPI router for metrics endpoint
from fastapi import FastAPI, APIRouter

router = APIRouter()

@router.get("/metrics")
async def metrics_endpoint() -> Dict[str, Any]:
    """Return metrics snapshot.
    
    Returns:
        Dictionary with status and metrics
    """
    return {
        "status": "ok",
        "metrics": collector.snapshot()
    }

# Helper function for testing (not used in production)
def get_router() -> APIRouter:
    """Get the metrics router (for testing only)."""
    return router
