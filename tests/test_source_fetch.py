"""Unit tests for core/source_fetch.py (Refactor item 5)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.db import get_engine, source_snapshots
from core.source_fetch import (
    FetchOutcome,
    TRANSPORT_FAILURE_STATUS,
    content_hash,
    fetch_and_record,
    fetch_and_record_sync,
    record_fetch_failure,
    record_fetch_success,
)


@pytest.fixture
def engine(tmp_path: Path):
    return get_engine(f"sqlite:///{tmp_path / 'test.db'}")


class _FakeClient:
    """Minimal HttpClient-shaped fake. Async fetch() returns whatever
    response object the test queued for the URL, or raises the
    queued exception."""

    def __init__(self, responses: dict):
        self._responses = responses

    async def fetch(self, url: str):
        item = self._responses.get(url)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status: int, text: str = "", final_url: str | None = None):
    return SimpleNamespace(status=status, text=text, final_url=final_url)


# ----- content_hash -----


def test_content_hash_is_deterministic() -> None:
    a = content_hash("hello world")
    b = content_hash("hello world")
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_content_hash_distinguishes_inputs() -> None:
    assert content_hash("a") != content_hash("b")


# ----- record_fetch_success -----


def test_record_fetch_success_inserts_new_snapshot(engine) -> None:
    sid = record_fetch_success(
        engine, source_url="https://a.example/page",
        text="real body content",
        final_url="https://a.example/page",
        stage="test_stage",
    )
    assert sid > 0
    with engine.begin() as conn:
        row = conn.execute(
            select(source_snapshots).where(
                source_snapshots.c.snapshot_id == sid
            )
        ).first()
    assert row.source_url == "https://a.example/page"
    assert row.http_status == 200
    assert row.extracted_text == "real body content"
    assert row.fetched_during_stage == "test_stage"


def test_record_fetch_success_dedups_on_same_url_and_hash(engine) -> None:
    a = record_fetch_success(
        engine, source_url="https://a.example/p", text="same body",
        final_url=None, stage="test",
    )
    b = record_fetch_success(
        engine, source_url="https://a.example/p", text="same body",
        final_url=None, stage="test",
    )
    assert a == b  # dedup returns existing snapshot_id


def test_record_fetch_success_different_text_creates_new_row(engine) -> None:
    """Same URL but new body content -> new row. This is how we detect
    site changes between re-fetches."""
    a = record_fetch_success(
        engine, source_url="https://a.example/p", text="v1",
        final_url=None, stage="test",
    )
    b = record_fetch_success(
        engine, source_url="https://a.example/p", text="v2",
        final_url=None, stage="test",
    )
    assert a != b


# ----- record_fetch_failure -----


def test_record_fetch_failure_writes_audit_row(engine) -> None:
    sid = record_fetch_failure(
        engine, source_url="https://a.example/x",
        http_status=500, final_url=None,
        note="HTTP 500", stage="test",
    )
    assert sid is not None
    with engine.begin() as conn:
        row = conn.execute(
            select(source_snapshots).where(
                source_snapshots.c.snapshot_id == sid
            )
        ).first()
    assert row.http_status == 500
    assert row.extracted_text is None


def test_record_fetch_failure_dedups_returns_none(engine) -> None:
    """Re-recording the SAME failure (same URL + status + note) hits
    the UNIQUE collision and returns None rather than churning rows."""
    record_fetch_failure(
        engine, source_url="https://a.example/x",
        http_status=500, final_url=None, note="same", stage="test",
    )
    second = record_fetch_failure(
        engine, source_url="https://a.example/x",
        http_status=500, final_url=None, note="same", stage="test",
    )
    assert second is None


def test_record_fetch_failure_different_note_creates_separate_row(engine):
    """500 with note X vs 500 with note Y are distinct events --
    different content_hash, so both rows land."""
    a = record_fetch_failure(
        engine, source_url="https://a.example/x",
        http_status=500, final_url=None, note="noteA", stage="test",
    )
    b = record_fetch_failure(
        engine, source_url="https://a.example/x",
        http_status=500, final_url=None, note="noteB", stage="test",
    )
    assert a != b


# ----- fetch_and_record (async + sync) -----


def test_fetch_and_record_success_writes_snapshot(engine) -> None:
    client = _FakeClient({
        "https://a.example/p": _resp(200, "real body",
                                      final_url="https://a.example/p"),
    })
    out = asyncio.run(fetch_and_record(
        engine, client, "https://a.example/p", stage="test",
    ))
    assert out.ok
    assert out.status == 200
    assert out.text == "real body"
    assert out.final_url == "https://a.example/p"
    assert out.snapshot_id is not None
    assert out.error is None


def test_fetch_and_record_transport_error_writes_failure(engine) -> None:
    client = _FakeClient({
        "https://a.example/p": ConnectionError("DNS lookup failed"),
    })
    out = asyncio.run(fetch_and_record(
        engine, client, "https://a.example/p", stage="test",
    ))
    assert not out.ok
    assert out.status == TRANSPORT_FAILURE_STATUS
    assert out.text is None
    assert "ConnectionError" in (out.error or "")
    assert "DNS lookup failed" in (out.error or "")
    assert out.snapshot_id is not None  # failure row landed


def test_fetch_and_record_http_error_writes_failure(engine) -> None:
    client = _FakeClient({
        "https://a.example/p": _resp(404, "", final_url=None),
    })
    out = asyncio.run(fetch_and_record(
        engine, client, "https://a.example/p", stage="test",
    ))
    assert not out.ok
    assert out.status == 404
    assert out.error == "HTTP 404"
    assert out.snapshot_id is not None


def test_fetch_and_record_empty_body_treated_as_failure(engine) -> None:
    """200 with whitespace-only body is a failure -- the LLM has
    nothing to extract from. Audit captures the empty body."""
    client = _FakeClient({
        "https://a.example/p": _resp(200, "   \n  ", final_url=None),
    })
    out = asyncio.run(fetch_and_record(
        engine, client, "https://a.example/p", stage="test",
    ))
    assert not out.ok
    assert out.error == "HTTP 200"
    assert out.text is None


def test_fetch_and_record_sync_wraps_async(engine) -> None:
    client = _FakeClient({
        "https://a.example/p": _resp(200, "body"),
    })
    out = fetch_and_record_sync(
        engine, client, "https://a.example/p", stage="test",
    )
    assert out.ok
    assert isinstance(out, FetchOutcome)


def test_fetch_outcome_ok_property() -> None:
    success = FetchOutcome(
        url="x", final_url=None, status=200, text="hi",
        snapshot_id=1, error=None,
    )
    fail = FetchOutcome(
        url="x", final_url=None, status=-1, text=None,
        snapshot_id=1, error="boom",
    )
    assert success.ok is True
    assert fail.ok is False
