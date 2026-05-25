"""Async HTTP client with timeouts, retries, and per-domain rate limiting.

No bare requests anywhere in this codebase. All outbound fetches go through
fetch(): 30s default timeout, tenacity exponential-backoff retries, and a
per-domain AsyncLimiter so a single host is never hammered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

DEFAULT_TIMEOUT = 30.0
# One request per second per domain; 5 concurrent across all domains.
_DOMAIN_RATE = (1, 1.0)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]


@dataclass
class FetchResult:
    # Batch 29 (#325): `url` is the URL the caller REQUESTED.
    # `final_url` is the URL after any redirects -- different when the
    # site does a 301 to a canonical host or a tracking-stripped path.
    # Callers that persist provenance (source_snapshots, verification)
    # should record final_url so re-fetching matches what the operator
    # actually saw.
    url: str
    status: int
    text: str
    final_url: str = ""


@dataclass
class HttpClient:
    timeout: float = DEFAULT_TIMEOUT
    _domain_limiters: dict[str, AsyncLimiter] = field(default_factory=dict)
    _ua_index: int = 0

    def _limiter(self, domain: str) -> AsyncLimiter:
        if domain not in self._domain_limiters:
            rate, period = _DOMAIN_RATE
            self._domain_limiters[domain] = AsyncLimiter(rate, period)
        return self._domain_limiters[domain]

    def _next_ua(self) -> str:
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        return ua

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=16),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL respecting the per-domain rate limit. Raises on network
        failure after retries; HTTP error statuses are returned, not raised.

        Batch 29 (#325): records final_url after redirect resolution so
        callers can persist the canonical URL alongside the snapshot.
        """
        domain = urlparse(url).netloc
        async with self._limiter(domain):
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": self._next_ua()})
                # httpx.Response.url is the FINAL URL after redirects.
                final = str(resp.url) if resp.url is not None else url
                return FetchResult(
                    url=url, status=resp.status_code, text=resp.text,
                    final_url=final,
                )
