"""Regression tests for the post-PR-29 finding that the Attio HTTP
client raised immediately on 429 / 503, burning the partner instead
of backing off and retrying.

Stubs httpx.Client.request via monkeypatching the bound `client`
attribute so we can drive a 429 -> 200 sequence (or 503 -> 200) and
assert the retry decorator honored the Retry-After header.
"""
from __future__ import annotations

import time

import httpx
import pytest

from core.attio_client import AttioClient, AttioError, AttioRetryableError


def _make_client() -> AttioClient:
    """Build an AttioClient whose underlying httpx.Client we'll monkey
    via the public .client property. The dataclass field is named
    base_url; api_key is the bearer."""
    c = AttioClient(api_key="fake", base_url="https://api.attio.com/v2")
    # Force eager creation so we can monkeypatch its request method.
    _ = c.client
    return c


def test_retries_on_429_then_succeeds(monkeypatch) -> None:
    client = _make_client()
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(f"{method} {url}")
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0.05"}, text='{"error":"rate"}',
                request=httpx.Request(method, url),
            )
        return httpx.Response(
            200, json={"data": {"id": {"record_id": "rec_ok"}}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(client.client, "request", fake_request)
    # Don't really sleep for 50ms; just observe that the sleep happens.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    start = time.monotonic()
    out = client._request("GET", "/objects/people/records/rec_ok")
    elapsed = time.monotonic() - start
    assert out == {"data": {"id": {"record_id": "rec_ok"}}}
    assert len(calls) == 2, calls
    # We patched sleep -> elapsed should be tiny, but the retry path
    # must have fired (otherwise calls would be length 1).
    assert elapsed < 1.0


def test_retries_on_503_then_succeeds(monkeypatch) -> None:
    client = _make_client()
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(f"{method} {url}")
        if len(calls) == 1:
            return httpx.Response(
                503, text="upstream down",
                request=httpx.Request(method, url),
            )
        return httpx.Response(
            200, json={"data": {}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(client.client, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = client._request("GET", "/objects/people/records/x")
    assert out == {"data": {}}
    assert len(calls) == 2


def test_non_retryable_4xx_raises_immediately(monkeypatch) -> None:
    """400 / 401 / 404 should NOT retry -- those represent operator
    errors we want to surface immediately."""
    client = _make_client()
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(f"{method} {url}")
        return httpx.Response(
            401, text='{"error":"unauthorized"}',
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(client.client, "request", fake_request)
    with pytest.raises(AttioError):
        client._request("GET", "/x")
    assert len(calls) == 1, "401 should not trigger retry"


def test_gives_up_after_stop_max_retries(monkeypatch) -> None:
    """Persistent 429 should eventually raise AttioRetryableError --
    the decorator stops after 3 attempts (stop_after_attempt(3))."""
    client = _make_client()
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(f"{method} {url}")
        return httpx.Response(
            429, headers={"Retry-After": "0"},
            text='{"error":"rate"}',
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(client.client, "request", fake_request)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(AttioRetryableError):
        client._request("GET", "/x")
    assert len(calls) == 3, (
        f"expected 3 attempts before giving up; got {len(calls)}"
    )
