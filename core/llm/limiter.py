"""Process-wide rate limiter singletons for shared API providers.

Two flavors of singleton, one per concurrency model:
  - `get_limiter(provider)` returns the async `AsyncLimiter` (used by the async
    `core.http_client.HttpClient`).
  - `get_sync_limiter(provider)` returns a thread-safe sync `SyncRateLimiter`
    (used by the sync `core.llm.client.LLMClient`).

Both are module-level singletons keyed by provider name, so concurrent
workspaces in the same process share a single bucket per provider and never
contend into 429s.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from aiolimiter import AsyncLimiter

# Conservative defaults: (max_calls, period_seconds). Tune per account tier.
_DEFAULTS: dict[str, tuple[float, float]] = {
    "anthropic": (50, 60.0),
    "listennotes": (20, 60.0),
}

_ASYNC_LIMITERS: dict[str, AsyncLimiter] = {}
_SYNC_LIMITERS: dict[str, "SyncRateLimiter"] = {}


class SyncRateLimiter:
    """Sliding-window rate limiter for sync code paths.

    `acquire()` blocks if the bucket is full, then records the call. The
    underlying deque is guarded by a re-entrant lock so concurrent threads in
    the same process share the same bucket. There is no global event loop, so
    aiolimiter (async-only) does not fit here.
    """

    def __init__(self, max_calls: int, period_s: float):
        self.max_calls = int(max_calls)
        self.period_s = float(period_s)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps outside the rolling window.
                while self._calls and now - self._calls[0] >= self.period_s:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                # Bucket full; sleep until the oldest call ages out.
                wait_s = self.period_s - (now - self._calls[0])
            if wait_s > 0:
                time.sleep(wait_s)


def get_limiter(provider: str) -> AsyncLimiter:
    """Return the process-wide async limiter for a provider."""
    key = provider.lower()
    if key not in _ASYNC_LIMITERS:
        rate, period = _DEFAULTS.get(key, (30, 60.0))
        _ASYNC_LIMITERS[key] = AsyncLimiter(rate, period)
    return _ASYNC_LIMITERS[key]


def get_sync_limiter(provider: str) -> SyncRateLimiter:
    """Return the process-wide sync limiter for a provider."""
    key = provider.lower()
    if key not in _SYNC_LIMITERS:
        rate, period = _DEFAULTS.get(key, (30, 60.0))
        _SYNC_LIMITERS[key] = SyncRateLimiter(int(rate), period)
    return _SYNC_LIMITERS[key]
