"""Thin synchronous wrapper around the Attio v2 REST API.

Auth: Bearer {ATTIO_API_KEY}. Base URL configurable via workspace attio.yaml
(default https://api.attio.com/v2). All calls are retried on transient
network failures via tenacity; 4xx responses raise immediately with the body.

The wrapper is deliberately thin: a sync httpx.Client per instance, no caching,
no schema validation of payload shapes. Callers compose payloads from the
workspace attio.yaml attribute map.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt


class AttioError(RuntimeError):
    """Raised on any non-2xx response."""


class AttioRetryableError(AttioError):
    """Raised for rate-limit/transient HTTP statuses that should be retried."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class AttioNotConfigured(RuntimeError):
    """Raised when an AttioClient is asked for, but the workspace lacks config."""


# Batch 18 (#653/#654/#655): refuse to send Attio's bearer token to any
# host outside this allowlist. A typo in attio.yaml's `api_base`, or a
# malicious config injection, would otherwise leak the key. Operators
# who legitimately self-host an Attio-compatible service can pass
# allow_any_base_url=True at construction.
ALLOWED_API_BASE_HOSTS = {
    "api.attio.com",
}


def _retry_after_s(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _attio_retry_wait_s(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, AttioRetryableError) and exc.retry_after_s is not None:
        return exc.retry_after_s
    return min(16.0, 2.0 * (2 ** max(0, retry_state.attempt_number - 1)))


@dataclass
class AttioClient:
    api_key: str
    base_url: str = "https://api.attio.com/v2"
    timeout_s: float = 30.0
    allow_any_base_url: bool = False
    _client: Optional[httpx.Client] = None

    def __post_init__(self) -> None:
        # Validate the base URL's host against the allowlist. Operators
        # self-hosting an Attio-compatible service can opt out by passing
        # allow_any_base_url=True (with full awareness that their bearer
        # token will be sent to that host).
        from urllib.parse import urlparse
        host = (urlparse(self.base_url).hostname or "").lower()
        if not host:
            raise AttioNotConfigured(
                f"Attio api_base {self.base_url!r} has no parseable host"
            )
        if not self.allow_any_base_url and host not in ALLOWED_API_BASE_HOSTS:
            raise AttioNotConfigured(
                f"Attio api_base host {host!r} not in allowlist "
                f"{sorted(ALLOWED_API_BASE_HOSTS)}. Set "
                f"allow_any_base_url=True to override (the bearer token "
                f"will be sent to {host!r})."
            )

    @classmethod
    def from_workspace(cls, ws) -> "AttioClient":
        """Build from a Workspace. Raises AttioNotConfigured if absent."""
        cfg = ws.attio or {}
        api_key = ws.env("ATTIO_API_KEY")
        if not api_key:
            raise AttioNotConfigured(
                "ATTIO_API_KEY not set in workspace .env (or root .env)"
            )
        attio_cfg = cfg.get("attio") or cfg  # accept either shape
        base = attio_cfg.get("api_base", "https://api.attio.com/v2")
        # Operators can opt out of the allowlist by setting
        # api_base_allow_any_host: true in attio.yaml.
        allow_any = bool(attio_cfg.get("api_base_allow_any_host", False))
        return cls(
            api_key=api_key, base_url=base,
            allow_any_base_url=allow_any,
        )

    # --- low-level transport ---

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_s,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=lambda retry_state: _attio_retry_wait_s(retry_state),
        retry=retry_if_exception_type((
            httpx.TransportError,
            httpx.TimeoutException,
            AttioRetryableError,
        )),
        reraise=True,
    )
    def _request(self, method: str, path: str, *, json_body: Any = None) -> dict:
        r = self.client.request(method, path, json=json_body)
        if r.status_code in {429, 503}:
            raise AttioRetryableError(
                f"Attio {method} {path} -> {r.status_code}: {r.text[:500]}",
                retry_after_s=_retry_after_s(r),
            )
        if r.status_code >= 400:
            raise AttioError(
                f"Attio {method} {path} -> {r.status_code}: {r.text[:500]}"
            )
        if not r.text:
            return {}
        try:
            return r.json()
        except json.JSONDecodeError as exc:
            # An empty body (no content) is fine -- some endpoints legitimately
            # return 200 with no body -- but a non-empty body that fails to
            # decode is a real protocol problem. Surface it as an AttioError
            # instead of returning {} and letting downstream log a successful
            # sync with attio_id=None.
            raise AttioError(
                f"Attio {method} {path} returned 200 with undecodable JSON: "
                f"{exc}"
            ) from exc

    # --- schema introspection ---

    def list_attributes(self, object_slug: str) -> list[dict]:
        """GET /v2/objects/{object}/attributes -> list of attribute dicts."""
        result = self._request("GET", f"/objects/{object_slug}/attributes")
        return result.get("data", []) if isinstance(result, dict) else result

    def attribute_slugs(self, object_slug: str) -> set[str]:
        """Set of api_slug values on a given object."""
        attrs = self.list_attributes(object_slug)
        return {a.get("api_slug") for a in attrs if a.get("api_slug")}

    # --- record CRUD ---

    def upsert_record(
        self,
        object_slug: str,
        matching_attribute: str,
        values: dict,
    ) -> dict:
        """PUT /v2/objects/{object}/records — assert by matching_attribute."""
        body = {
            "data": {
                "values": values,
            }
        }
        return self._request(
            "PUT",
            f"/objects/{object_slug}/records?matching_attribute={matching_attribute}",
            json_body=body,
        )

    def create_record(self, object_slug: str, values: dict) -> dict:
        return self._request(
            "POST",
            f"/objects/{object_slug}/records",
            json_body={"data": {"values": values}},
        )

    def update_record(self, object_slug: str, record_id: str, values: dict) -> dict:
        return self._request(
            "PATCH",
            f"/objects/{object_slug}/records/{record_id}",
            json_body={"data": {"values": values}},
        )

    def query_records(
        self,
        object_slug: str,
        filter_body: dict,
        limit: int = 1,
        offset: int = 0,
    ) -> list[dict]:
        body = {"filter": filter_body, "limit": limit}
        if offset:
            body["offset"] = offset
        result = self._request(
            "POST",
            f"/objects/{object_slug}/records/query",
            json_body=body,
        )
        data = result.get("data", []) if isinstance(result, dict) else result
        return data if isinstance(data, list) else []

    def query_records_all(
        self,
        object_slug: str,
        filter_body: dict,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict]:
        """Paginated wrapper: pulls until a page returns fewer than page_size
        rows, or until max_pages is hit (defensive). Brief Rule 14 forbids
        silent truncation; outcome sync was previously capped at one page.
        """
        out: list[dict] = []
        for page in range(max_pages):
            chunk = self.query_records(
                object_slug, filter_body,
                limit=page_size, offset=page * page_size,
            )
            out.extend(chunk)
            if len(chunk) < page_size:
                break
        else:
            # Hit max_pages without seeing a short page -- likely more data.
            # Raise so the caller logs it loudly instead of silently
            # dropping outcomes.
            raise AttioError(
                f"query_records_all hit max_pages={max_pages} for "
                f"{object_slug!r}; results probably truncated"
            )
        return out

    def get_record(self, object_slug: str, record_id: str) -> dict:
        result = self._request(
            "GET", f"/objects/{object_slug}/records/{record_id}"
        )
        return result.get("data", {}) if isinstance(result, dict) else result

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
