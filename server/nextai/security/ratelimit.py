from __future__ import annotations

import threading
import time


class RateLimiter:
    """In-memory token buckets keyed by string. Thread-safe."""

    def __init__(self, max_keys: int = 50000):
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, rate_per_second: float, burst: float, cost: float = 1.0) -> tuple[bool, float]:
        """Consume `cost` tokens. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (burst, now))
            tokens = min(burst, tokens + (now - last) * rate_per_second)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                allowed, retry = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed = False
                retry = (cost - tokens) / rate_per_second if rate_per_second > 0 else 60.0
            if len(self._buckets) > self._max_keys:
                self._evict(now, rate_per_second, burst)
        return allowed, retry

    def _evict(self, now: float, rate: float, burst: float) -> None:
        full_after = burst / rate if rate > 0 else 3600
        stale = [k for k, (_, last) in self._buckets.items() if now - last > full_after]
        for k in stale:
            del self._buckets[k]

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
