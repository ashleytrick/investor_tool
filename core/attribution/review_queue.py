"""Generic review_items queue (Slice 6).

Adds a row to the `review_items` table for any kind of item that
needs human attention. The `kind` discriminator lets a future UI
show all pending reviews in one place even though different kinds
have different per-item context shapes.

Used by Stage 3 (ambiguous_attribution) today. Other kinds plug
in via the same interface.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from core.db import review_items


KIND_AMBIGUOUS_ATTRIBUTION = "ambiguous_attribution"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def queue_review(
    engine: Any,
    *,
    kind: str,
    target_id: str,
    context: dict | None = None,
) -> int:
    """Insert a pending review_items row. Idempotent on
    (kind, target_id, status='pending'): a second call for the same
    target is a no-op (returns the existing review_id) so retries
    don't pile up duplicate review tasks."""
    with engine.begin() as conn:
        existing = conn.execute(
            select(review_items.c.review_id).where(
                review_items.c.kind == kind,
                review_items.c.target_id == str(target_id),
                review_items.c.status == "pending",
            ).limit(1)
        ).first()
        if existing is not None:
            return int(existing.review_id)
        result = conn.execute(review_items.insert().values(
            kind=kind,
            target_id=str(target_id),
            context=json.dumps(context or {}, default=str),
            status="pending",
            created_at=_now(),
        ))
        return int(result.inserted_primary_key[0])


def list_pending(engine: Any, *, kind: str | None = None) -> list[Any]:
    """Pending review rows, most recent first. `kind` filters when
    supplied."""
    with engine.begin() as conn:
        stmt = select(review_items).where(review_items.c.status == "pending")
        if kind is not None:
            stmt = stmt.where(review_items.c.kind == kind)
        stmt = stmt.order_by(review_items.c.review_id.desc())
        return list(conn.execute(stmt))


def resolve(
    engine: Any,
    *,
    review_id: int,
    resolved_by: str,
    notes: str | None = None,
    status: str = "resolved",
) -> None:
    """Mark a review row as resolved or dismissed. Idempotent --
    re-resolving an already-resolved row is a no-op."""
    if status not in ("resolved", "dismissed"):
        raise ValueError(f"unknown resolve status {status!r}")
    with engine.begin() as conn:
        conn.execute(
            update(review_items)
            .where(
                review_items.c.review_id == review_id,
                review_items.c.status == "pending",
            )
            .values(
                status=status,
                resolved_by=resolved_by,
                resolved_at=_now(),
                resolution_notes=notes,
            )
        )
