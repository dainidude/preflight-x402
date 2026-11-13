"""Thread-safe funnel metrics for Preflight API."""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

# Global start time for uptime calculation
_START_TIME = time.time()

_FLUSH_INTERVAL_S = 2.0


class MetricsCollector:
    """Thread-safe metrics collector for Preflight API.

    When STATE_DIR is set, counters restore from <state_dir>/metrics.json on
    init and flush after mutations (throttled to one write per 2s). Without
    STATE_DIR the behavior is byte-identical to the original in-memory one.
    """

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
        self._first_settle_at: str | None = None
        self._last_settle_at: str | None = None
        self._auth_errors_total = 0
        self._scans_by_agent: Dict[str, int] = {}
        self._revenue_by_agent: Dict[str, int] = {}
        self._last_flush = 0.0
        self._state_file = self._state_path()
        self._maybe_restore()

    @staticmethod
    def _state_path() -> Path | None:
        import os

        d = os.getenv("STATE_DIR")
        return Path(d) / "metrics.json" if d else None

    def _maybe_restore(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self._scans_total = data.get("scans_total", 0)
            self._scans_by_verdict = dict(data.get("scans_by_verdict", {}))
            self._scans_paid = data.get("scans_paid", 0)
            self._scans_free = data.get("scans_free", 0)
            self._settlements_total = data.get("settlements_total", 0)
            self._revenue_base_units = data.get("revenue_base_units", 0)
            self._unique_payers = {k: True for k in data.get("unique_payers", [])}
            self._free_tier_grants = dict(data.get("free_tier_grants", {}))
            self._rate_limited_total = data.get("rate_limited_total", 0)
            self._payment_required_total = dict(data.get("payment_required_total", {}))
            self._first_settle_at = data.get("first_settle_at")
            self._last_settle_at = data.get("last_settle_at")
            self._auth_errors_total = data.get("auth_errors_total", 0)
            self._scans_by_agent = dict(data.get("scans_by_agent", {}))
            self._revenue_by_agent = dict(data.get("revenue_by_agent", {}))

    def _maybe_flush(self, force: bool = False) -> None:
        """Throttled atomic snapshot write; never raises. force=True skips the window.

        The snapshot payload is built INSIDE the lock so concurrent record_*
        calls can't tear it (grill finding #6); file I/O happens outside.
        """
        if not self._state_file:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_flush < _FLUSH_INTERVAL_S:
                return
            self._last_flush = now
            payload = json.dumps(
                {
                    "scans_total": self._scans_total,
                    "scans_by_verdict": self._scans_by_verdict,
                    "scans_paid": self._scans_paid,
                    "scans_free": self._scans_free,
                    "settlements_total": self._settlements_total,
                    "revenue_base_units": self._revenue_base_units,
                    "unique_payers": list(self._unique_payers.keys()),
                    "free_tier_grants": self._free_tier_grants,
                    "rate_limited_total": self._rate_limited_total,
                    "payment_required_total": self._payment_required_total,
                    "first_settle_at": self._first_settle_at,
                    "last_settle_at": self._last_settle_at,
                    "auth_errors_total": self._auth_errors_total,
                    "scans_by_agent": {
                        k: v for k, v in sorted(
                            self._scans_by_agent.items(),
                            key=lambda kv: self._revenue_by_agent.get(kv[0], 0),
                            reverse=True,
                        )[:1000]
                    },
                    "revenue_by_agent": dict(sorted(
                        self._revenue_by_agent.items(), key=lambda kv: kv[1], reverse=True
                    )[:1000]),
                }
            )
        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(payload)
            tmp.replace(self._state_file)
        except OSError:
            pass

    
    @staticmethod
    def _agent_key(agent: str | None) -> str | None:
        """Mask agent identities in stored/snapshot form: 0xc592…f30B."""
        if not agent:
            return None
        return agent if "…" in agent else (agent[:6] + "…" + agent[-4:] if len(agent) > 12 else agent)

    def _bump_agent(self, agent: str | None, scans: int = 0, revenue: int = 0) -> None:
        key = self._agent_key(agent)
        if not key:
            return
        self._scans_by_agent[key] = self._scans_by_agent.get(key, 0) + scans
        self._revenue_by_agent[key] = self._revenue_by_agent.get(key, 0) + revenue
        if len(self._revenue_by_agent) > 1000:  # bounded memory — evict by REVENUE
            poorest = min(self._revenue_by_agent, key=self._revenue_by_agent.get)
            self._revenue_by_agent.pop(poorest, None)
            self._scans_by_agent.pop(poorest, None)

    def record_scan(self, route: str, verdict: str, paid: bool, agent: str | None = None) -> None:
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
            self._bump_agent(agent, scans=1)
        self._maybe_flush()

    def record_settle(
        self, route: str, amount_base_units: int, payer: str | None, agent: str | None = None
    ) -> None:
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
            self._bump_agent(agent, revenue=amount_base_units)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._last_settle_at = stamp
            if self._first_settle_at is None:
                self._first_settle_at = stamp
        self._maybe_flush(force=True)  # revenue never waits for the throttle window

    def record_free(self, route: str) -> None:
        """Record a free-tier grant (per-route counter; scan counts come from record_scan).

        Args:
            route: The API route that was served free of charge
        """
        with self._lock:
            self._free_tier_grants[route] = self._free_tier_grants.get(route, 0) + 1
        self._maybe_flush()

    def record_rate_limited(self) -> None:
        """Record a rate limited request."""
        with self._lock:
            self._rate_limited_total += 1
        self._maybe_flush()

    def record_401(self, route: str) -> None:
        """Record a rejected API-key attempt (optional key layer)."""
        with self._lock:
            self._auth_errors_total += 1
        self._maybe_flush()

    def record_402(self, route: str) -> None:
        """Record a 402 payment required response.

        Args:
            route: The API route that returned 402
        """
        with self._lock:
            self._payment_required_total[route] = self._payment_required_total.get(route, 0) + 1
        self._maybe_flush()

    def snapshot(self) -> Dict[str, Any]:
        """Get current metrics snapshot.
        
        Returns:
            Dictionary with all metrics fields
        """
        with self._lock:
            # Calculate uptime
            uptime_s = time.time() - _START_TIME
            top_agents = sorted(
                self._revenue_by_agent.items(), key=lambda kv: kv[1], reverse=True
            )[:10]

            return {
                "uptime_s": uptime_s,
                "first_settle_at": self._first_settle_at,
                "last_settle_at": self._last_settle_at,
                "auth_errors_total": self._auth_errors_total,
                "agents_total": len(self._revenue_by_agent),
                "top_agents": [
                    {
                        "agent": a,
                        "scans": self._scans_by_agent.get(a, 0),
                        "revenue_base_units": r,
                    }
                    for a, r in top_agents
                ],
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

@router.api_route("/metrics", methods=["GET", "HEAD"])
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
