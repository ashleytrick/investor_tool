"""Sources registry (Slice 18b / REFACTOR_PLAN item 21).

`source_url` strings live across multiple tables (source_snapshots,
signals, deal_attributions, ambiguous_matches, funds.source_urls).
This module owns the new canonical `sources` table -- one row per
unique URL with a stable integer `source_id`. The migration to FK
references is staged across multiple slices; for now this module
exposes the upsert primitive + a read helper.

Usage:

    from core.sources import upsert_source

    with engine.begin() as conn:
        sid = upsert_source(
            conn, source_url="https://news.example/ledgerkit-seed",
            source_type="funding_announcement_feed",
        )
        conn.execute(source_snapshots.insert().values(
            source_url=url, source_id=sid, ...
        ))

The function is idempotent: a second call for the same URL returns
the existing source_id + bumps last_seen_at on the row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from core.db import sources


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_source(
    conn: Any,
    *,
    source_url: str,
    source_type: str | None = None,
) -> int:
    """Return the canonical source_id for `source_url`. Creates the
    row when absent; otherwise bumps last_seen_at + opportunistically
    fills in source_type when previously NULL.

    Caller must pass a SQLAlchemy connection inside an active
    transaction (so this function can compose with other writes in
    the same atomic unit). When source_type is supplied, it's
    written; when omitted on a fresh row, source_type stays NULL.
    """
    now = _now()
    existing = conn.execute(
        select(sources.c.source_id, sources.c.source_type)
        .where(sources.c.source_url == source_url)
    ).first()
    if existing is not None:
        values: dict[str, Any] = {"last_seen_at": now}
        # Fill source_type when the existing row didn't know it.
        # Don't OVERWRITE a known type with a different one -- the
        # caller can't unilaterally re-categorize a URL; that's
        # an operator decision.
        if source_type and not existing.source_type:
            values["source_type"] = source_type
        conn.execute(
            update(sources)
            .where(sources.c.source_id == existing.source_id)
            .values(**values)
        )
        return int(existing.source_id)
    result = conn.execute(sources.insert().values(
        source_url=source_url,
        source_type=source_type,
        first_seen_at=now,
        last_seen_at=now,
    ))
    return int(result.inserted_primary_key[0])


def find_source_id(conn: Any, source_url: str) -> int | None:
    """Lookup-only variant: return the existing source_id for
    `source_url`, or None if not registered. Used by readers that
    don't want to mutate `last_seen_at`."""
    row = conn.execute(
        select(sources.c.source_id)
        .where(sources.c.source_url == source_url)
    ).first()
    return int(row.source_id) if row else None
