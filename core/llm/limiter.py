"""Process-wide rate limiter singletons for shared API providers.

Shared providers (Anthropic above all) are rate-limited at the PROCESS level so
concurrent workspace runs in the same process never contend into 429s. Each
provider gets exactly one AsyncLimiter for the life of the process.
"""
from __future__ import annotations

from aiolimiter import AsyncLimiter

# Conservative default: requests per 60s window. Tune per account tier.
_DEFAULTS: dict[str, tuple[float, float]] = {
    "anthropic": (50, 60.0),
    "listennotes": (20, 60.0),
}

_LIMITERS: dict[str, AsyncLimiter] = {}


def get_limiter(provider: str) -> AsyncLimiter:
    """Return the process-wide limiter for a provider, creating it once."""
    key = provider.lower()
    if key not in _LIMITERS:
        rate, period = _DEFAULTS.get(key, (30, 60.0))
        _LIMITERS[key] = AsyncLimiter(rate, period)
    return _LIMITERS[key]
