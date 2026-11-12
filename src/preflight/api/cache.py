"""In-memory LRU cache for Preflight API."""

import time
from typing import Dict, Any, Optional
from threading import Lock
from collections import OrderedDict

class LRUCache:
    """Thread-safe LRU cache with TTL support."""
    
    def __init__(self, maxsize: int = 512, ttl: int = 3600):
        """Initialize LRU cache.
        
        Args:
            maxsize: Maximum number of entries to store
            ttl: Time to live in seconds (default: 1 hour)
        """
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache: Dict[str, Dict[str, Any]] = OrderedDict()
        self.lock = Lock()
        self.access_count = 0
        self.hit_count = 0
    
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get value from cache by key.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found or expired
        """
        with self.lock:
            self.access_count += 1
            
            # Check if key exists and is not expired
            if key not in self.cache:
                return None
            
            entry = self.cache[key]
            
            # Check TTL
            if time.time() - entry["timestamp"] > self.ttl:
                del self.cache[key]
                return None
            
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            self.hit_count += 1
            
            return entry["value"].copy()
    
    def put(self, key: str, value: Dict[str, Any]) -> None:
        """Put value into cache.
        
        Args:
            key: Cache key
            value: Value to store
        """
        with self.lock:
            # If key already exists, remove it first
            if key in self.cache:
                del self.cache[key]
            
            # Add new entry
            self.cache[key] = {
                "value": value,
                "timestamp": time.time()
            }
            
            # If cache is full, remove oldest entry
            if len(self.cache) > self.maxsize:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
    
    def clear(self) -> None:
        """Clear all entries from cache."""
        with self.lock:
            self.cache.clear()
            self.access_count = 0
            self.hit_count = 0
    
    def size(self) -> int:
        """Get current cache size.
        
        Returns:
            Number of entries in cache
        """
        with self.lock:
            return len(self.cache)
    
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics.
        
        Returns:
            Dictionary with cache statistics
        """
        with self.lock:
            hits = self.hit_count
            accesses = self.access_count
            size = len(self.cache)
            
            hit_rate = (hits / accesses * 100) if accesses > 0 else 0
            
            return {
                "size": size,
                "maxsize": self.maxsize,
                "hit_rate": hit_rate,
                "accesses": accesses,
                "hits": hits,
                "ttl_seconds": self.ttl
            }

# Global cache instance
_cache_instance: Optional[LRUCache] = None

def get_cache() -> LRUCache:
    """Get or create global cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = LRUCache(maxsize=512, ttl=3600)
    return _cache_instance

# Module-level convenience functions
_cache = get_cache()

def cache_get(key: str) -> Optional[Dict[str, Any]]:
    """Get value from cache (module-level function)."""
    return _cache.get(key)

def cache_put(key: str, value: Dict[str, Any]) -> None:
    """Put value into cache (module-level function)."""
    _cache.put(key, value)

def cache_clear() -> None:
    """Clear all entries from cache (module-level function)."""
    _cache.clear()

def cache_stats() -> Dict[str, Any]:
    """Get cache statistics (module-level function)."""
    return _cache.stats()
