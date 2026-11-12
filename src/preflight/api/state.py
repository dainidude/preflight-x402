"""Durable state for Preflight API - JSON file persistence."""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

class DurableState:
    """JSON-file persistence for metrics + usage counters.
    
    Thread-safe; save coalescing is fine (callers decide cadence).
    When dir is None or empty, works in-memory only (backwards compatible).
    """
    
    def __init__(self, dir: str | None) -> None:
        """Initialize DurableState.
        
        Args:
            dir: Directory path for persistence. If None or empty,
                 works in-memory only.
        """
        self._dir = dir
        self._lock = threading.RLock()
        self._pending_save = False
        self._save_thread = None
        self._state_file: Optional[Path] = None
        
        if self._dir:
            self._state_file = Path(self._dir) / "state.json"
            # Ensure directory exists
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def save(self, snapshot: Dict[str, Any]) -> None:
        """Save snapshot to persistent storage.
        
        Args:
            snapshot: Data to persist
        """
        if not self._dir:
            return
        
        with self._lock:
            # Write to temporary file for atomic replacement
            temp_file = self._state_file.with_suffix('.tmp')
            
            try:
                temp_file.write_text(json.dumps(snapshot, indent=2, default=str))
                # Atomic replace
                temp_file.replace(self._state_file)
            except (OSError, json.JSONEncodeError) as e:
                # Log error but don't crash the application
                # In production, you might want to use proper logging
                pass
            finally:
                # Clean up temp file if it exists
                if temp_file.exists() and temp_file != self._state_file:
                    try:
                        temp_file.unlink()
                    except OSError:
                        pass
    
    def load(self) -> Optional[Dict[str, Any]]:
        """Load snapshot from persistent storage.
        
        Returns:
            Loaded data or None if file doesn't exist or is corrupt
        """
        if not self._dir or not self._state_file:
            return None
        
        with self._lock:
            try:
                if not self._state_file.exists():
                    return None
                
                content = self._state_file.read_text()
                return json.loads(content)
            except (OSError, json.JSONDecodeError):
                return None

# Dead letter storage for failed settlements
class SettleDeadLetterStorage:
    """Storage for settlement dead letters."""
    
    def __init__(self, dir: str | None) -> None:
        self._dir = dir
        self._lock = threading.RLock()  # mark_resolved re-enters via read_all_dead_letters
        self._dead_letter_file: Optional[Path] = None
        self._resolved_file: Optional[Path] = None
        
        if self._dir:
            self._dead_letter_file = Path(self._dir) / "settle_deadletter.jsonl"
            self._resolved_file = Path(self._dir) / "settle_deadletter.resolved.jsonl"
            # Ensure directory exists
            self._dead_letter_file.parent.mkdir(parents=True, exist_ok=True)
    
    def append_dead_letter(self, entry: Dict[str, Any]) -> None:
        """Append a dead letter entry.
        
        Args:
            entry: Dead letter data to store
        """
        if not self._dir or not self._dead_letter_file:
            return
        
        with self._lock:
            try:
                with open(self._dead_letter_file, 'a') as f:
                    f.write(json.dumps(entry) + '\n')
            except OSError:
                pass
    
    def append_resolved(self, entry: Dict[str, Any]) -> None:
        """Append a resolved dead letter entry.
        
        Args:
            entry: Resolved dead letter data to store
        """
        if not self._dir or not self._resolved_file:
            return
        
        with self._lock:
            try:
                with open(self._resolved_file, 'a') as f:
                    f.write(json.dumps(entry) + '\n')
            except OSError:
                pass
    
    def read_all_dead_letters(self) -> list[Dict[str, Any]]:
        """Read all dead letter entries.
        
        Returns:
            List of dead letter entries
        """
        if not self._dir or not self._dead_letter_file:
            return []
        
        with self._lock:
            if not self._dead_letter_file.exists():
                return []

            try:
                entries = []
                with open(self._dead_letter_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(json.loads(line))
                return entries
            except (OSError, json.JSONDecodeError):
                return []
    
    def mark_resolved(self, index: int) -> bool:
        """Mark a dead letter entry as resolved.
        
        Args:
            index: Index of entry to mark as resolved
            
        Returns:
            True if successful, False otherwise
        """
        if not self._dir:
            return False
        
        with self._lock:
            try:
                entries = self.read_all_dead_letters()
                if index < 0 or index >= len(entries):
                    return False
                
                resolved_entry = entries.pop(index)
                self.append_resolved(resolved_entry)
                
                # Rewrite dead letter file without the resolved entry
                with open(self._dead_letter_file, 'w') as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + '\n')
                
                return True
            except (OSError, json.JSONDecodeError):
                return False

# Global instances
_state: Optional[DurableState] = None
_settle_dead_letter_storage: Optional[SettleDeadLetterStorage] = None

# Configuration
def configure(state_dir: str | None) -> None:
    """Configure the durable state.
    
    Args:
        state_dir: Directory path for persistent storage
    """
    global _state, _settle_dead_letter_storage
    
    _state = DurableState(state_dir)
    _settle_dead_letter_storage = SettleDeadLetterStorage(state_dir)

# Helper functions for public API
def get_state() -> Optional[DurableState]:
    """Get the durable state instance."""
    return _state

def get_settle_dead_letter_storage() -> Optional[SettleDeadLetterStorage]:
    """Get the settle dead letter storage instance."""
    return _settle_dead_letter_storage

# Digest formatting function
def format_digest(metrics: Dict[str, Any]) -> str:
    """Format metrics snapshot for digest.
    
    Args:
        metrics: Metrics snapshot
        
    Returns:
        Formatted digest string
    """
    lines = [
        "🚀 Preflight Daily Digest",
        "=======================",
        f"Uptime: {metrics.get('uptime_s', 0):.1f}s",
        f"Scans Total: {metrics.get('scans_total', 0)}",
        f"Scans Paid: {metrics.get('scans_paid', 0)}",
        f"Scans Free: {metrics.get('scans_free', 0)}",
        f"Settlements: {metrics.get('settlements_total', 0)}",
        f"Revenue USDC: ${metrics.get('revenue_usdc', 0):.2f}",
        f"Unique Payers: {metrics.get('unique_payers', 0)}",
        f"Payment Required: {sum(metrics.get('payment_required_total', {}).values())}",
        f"Rate Limited: {metrics.get('rate_limited_total', 0)}",
        "",
        "🔍 Top Findings:",
    ]
    
    scans_by_verdict = metrics.get('scans_by_verdict', {})
    for verdict, count in sorted(scans_by_verdict.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"  • {verdict}: {count}")
    
    lines.append("")
    from datetime import datetime, timezone

    lines.append(f"📊 Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    
    return '\n'.join(lines)
