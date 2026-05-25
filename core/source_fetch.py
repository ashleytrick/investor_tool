"""Shared source-fetch + snapshot helpers (Refactor item 5).

Stages 1-5 each fetch external URLs and need the same combination
of behaviors:

  - Transport-layer error -> `source_snapshots` row with http_status=-1
    so the audit captures the attempt.
  - HTTP error (status >= 400) -> snapshot row with the real status.
  - Successful 200 with non-empty body -> snapshot row with the
    extracted text + content_hash for dedup, final_url for post-
    redirect tracking.

Today each stage re-implements pieces of this -- Stage 2 has its own
`store_snapshots` over a dict of pre-fetched pages, Stage 4 has
`upsert_snapshot` + `upsert_snapshot_failure` inline. This module
collapses both into one shape:

  - `FetchOutcome` dataclass: the per-URL result
  - `content_hash(text)` : the sha256 used for snapshot dedup
  - `record_fetch_success(engine, url, text, ..., stage)` : insert /
    return-existing source_snapshots row for a successful fetch
  - `record_fetch_failure(engine, url, ..., stage)` : insert a failure
    snapshot row so the audit captures the attempted URL
  - `fetch_and_record(engine, client, url, stage, ...)` : the
    end-to-end fetch + record helper Stages 1-4 can call uniformly

Stages that need batch/parallel fetching (Stage 2 fans out to
LIVE_PATHS, Stage 4 iterates a CSV) keep their own outer loop but
delegate per-URL handling to this module.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.db import source_snapshots


# Sentinel http_status used in failure snapshots when no HTTP response
# was received (DNS error, connection refused, timeout, etc.). Picked
# to be unambiguously not-a-real-status so downstream queries can
# `WHERE http_status = -1` to find transport failures.
TRANSPORT_FAILURE_STATUS = -1


@dataclass(frozen=True)
class FetchOutcome:
    """Per-URL fetch result with both the HTTP layer + the snapshot
    layer collapsed into one struct.

    On success: status=200, text=non-empty, snapshot_id=<int>, error=None.
    On HTTP error: status=4xx/5xx, text=None, snapshot_id=<int>,
                   error="HTTP {status}".
    On transport error: status=-1, text=None, snapshot_id=<int|None>,
                       error="{ExcType}: {msg}".

    `snapshot_id` is None only when the failure-snapshot INSERT itself
    hit a unique-collision (already audited for this URL+hash); the
    operator can still find the prior row in source_snapshots.
    """
    url: str
    final_url: str | None
    status: int
    text: str | None
    snapshot_id: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.text is not None


def content_hash(text: str) -> str:
    """sha256(text) -- the dedup key for source_snapshots."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_fetch_success(
    engine: Any,
    *,
    source_url: str,
    text: str,
    final_url: str | None,
    stage: str,
    http_status: int = 200,
) -> int:
    """Insert (or return-existing) source_snapshots row for a successful
    fetch. Dedup key is (source_url, content_hash) so the same body
    re-fetched produces the same snapshot_id rather than churning rows.
    """
    chash = content_hash(text)
    with engine.begin() as conn:
        existing = conn.execute(
            select(source_snapshots.c.snapshot_id).where(
                source_snapshots.c.source_url == source_url,
                source_snapshots.c.content_hash == chash,
            )
        ).first()
        if existing:
            return int(existing.snapshot_id)
        result = conn.execute(source_snapshots.insert().values(
            source_url=source_url,
            final_url=final_url,
            fetched_at=_now(),
            http_status=http_status,
            content_hash=chash,
            extracted_text=text,
            fetched_during_stage=stage,
        ))
        return int(result.inserted_primary_key[0])


def record_fetch_failure(
    engine: Any,
    *,
    source_url: str,
    http_status: int,
    final_url: str | None,
    note: str,
    stage: str,
) -> int | None:
    """Insert a source_snapshots row for a failed fetch so the audit
    captures the attempt. extracted_text stays NULL; the content_hash
    is derived from the failure note so two distinct failure modes on
    the same URL produce distinct rows (and re-running the same
    failure deduplicates via UNIQUE constraint).

    Returns the inserted snapshot_id, or None when the insert hit the
    UNIQUE collision (the same failure was already audited).
    """
    chash = content_hash(f"FAIL:{http_status}:{note}")
    with engine.begin() as conn:
        try:
            result = conn.execute(source_snapshots.insert().values(
                source_url=source_url,
                final_url=final_url,
                fetched_at=_now(),
                http_status=http_status,
                content_hash=chash,
                extracted_text=None,
                fetched_during_stage=stage,
            ))
            return int(result.inserted_primary_key[0])
        except Exception:  # noqa: BLE001 - UNIQUE collision is expected
            return None


async def fetch_and_record(
    engine: Any,
    client: Any,
    url: str,
    *,
    stage: str,
) -> FetchOutcome:
    """One-call fetch + snapshot. Handles transport errors, non-200
    responses, and empty-body 200s as failure snapshots; writes a
    success snapshot only when the body is non-empty.

    `client` is an HttpClient-shaped object exposing async fetch(url).
    Returning a FetchOutcome rather than raising lets the caller's
    outer loop keep going + count failures via run.fail() / log_error.
    """
    try:
        res = await client.fetch(url)
    except Exception as exc:  # noqa: BLE001 - audited via FetchOutcome
        note = f"{type(exc).__name__}: {exc}"
        sid = record_fetch_failure(
            engine, source_url=url,
            http_status=TRANSPORT_FAILURE_STATUS,
            final_url=None, note=note, stage=stage,
        )
        return FetchOutcome(
            url=url, final_url=None, status=TRANSPORT_FAILURE_STATUS,
            text=None, snapshot_id=sid, error=note,
        )

    final_url = getattr(res, "final_url", None)
    if res.status != 200 or not (res.text or "").strip():
        note = f"HTTP {res.status}"
        sid = record_fetch_failure(
            engine, source_url=url, http_status=res.status,
            final_url=final_url, note=note, stage=stage,
        )
        return FetchOutcome(
            url=url, final_url=final_url, status=res.status,
            text=None, snapshot_id=sid, error=note,
        )

    sid = record_fetch_success(
        engine, source_url=url, text=res.text,
        final_url=final_url, stage=stage,
    )
    return FetchOutcome(
        url=url, final_url=final_url, status=res.status,
        text=res.text, snapshot_id=sid, error=None,
    )


def fetch_and_record_sync(
    engine: Any,
    client: Any,
    url: str,
    *,
    stage: str,
) -> FetchOutcome:
    """Synchronous wrapper around fetch_and_record() for callers
    outside an event loop. The pipeline scripts mostly run sync, so
    this saves an asyncio.run() at every call site."""
    return asyncio.run(fetch_and_record(engine, client, url, stage=stage))
