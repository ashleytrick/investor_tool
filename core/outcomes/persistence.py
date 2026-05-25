"""Outcome dedup + insert (Refactor item 16).

Source-neutral persistence layer for OutcomeEvent streams. Two
dedup paths run BEFORE the insert:

  1. external_event_id duplicate -- the exact same source observation
     was already ingested. Cron retries + webhook replays land here.
  2. unchanged state -- this event's meaningful fields match the
     latest outcome row for the same partner. Prevents an Attio
     touch-without-state-change from creating a no-op outcome row
     that pollutes the learning report's view of "latest state".

Both checks return True to mean "skip this event". The
`persist_outcome_event` function applies both then inserts, returning
the inserted outcome_id or None if the event was a dedup hit.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from core.db import outcomes
from core.outcomes.events import OutcomeEvent


def is_duplicate_event(engine: Any, external_event_id: str) -> bool:
    """True iff an outcomes row with this external_event_id already
    exists. The external_event_id index is the primary dedup key."""
    if not external_event_id:
        return False
    with engine.begin() as conn:
        row = conn.execute(
            select(outcomes.c.outcome_id).where(
                outcomes.c.external_event_id == external_event_id,
            )
        ).first()
    return row is not None


def is_unchanged_from_latest(engine: Any, event: OutcomeEvent) -> bool:
    """True iff the partner's most-recent outcome row has the same
    state fields as `event`. Protects against:

      - Attio touches that bump last_modified_at without changing any
        of the outcome columns (e.g. an unrelated tag edit).
      - Re-runs of older sync cycles that should not insert
        duplicate outcomes for already-observed state.

    The check excludes external_event_id deliberately: a different
    source observing the same state IS a no-op for the learning
    report; only the first one needs to land.
    """
    with engine.begin() as conn:
        latest = conn.execute(
            select(outcomes).where(
                outcomes.c.partner_id == event.partner_id,
            ).order_by(outcomes.c.outcome_id.desc()).limit(1)
        ).first()
    if latest is None:
        return False
    return (
        latest.outreach_status == event.outreach_status
        and latest.reply_type == event.reply_type
        and bool(latest.meeting_booked) == event.meeting_booked
        and latest.meeting_date == event.meeting_date
        and latest.meeting_outcome == event.meeting_outcome
    )


def persist_outcome_event(engine: Any, event: OutcomeEvent) -> int | None:
    """Apply both dedup checks and insert the event when neither
    fires. Returns the inserted outcome_id on success, or None when
    the event was a dedup hit.

    Both dedup queries + the insert happen against the same engine;
    each opens its own transaction so a concurrent inserter can't
    sneak between (the unique index on external_event_id is the
    final guard).
    """
    if is_duplicate_event(engine, event.external_event_id):
        return None
    if is_unchanged_from_latest(engine, event):
        return None
    with engine.begin() as conn:
        result = conn.execute(
            outcomes.insert().values(**event.to_row_values())
        )
        return int(result.inserted_primary_key[0])
