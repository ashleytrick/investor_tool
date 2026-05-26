"""Sequences router (FR-3): per-partner follow-up state machine.

  GET  /sequences/{partner_id}              read current sequence state
  POST /sequences/{sequence_id}/stop        {reason: 'user'|...}
  POST /sequences/{sequence_id}/skip        {days}

The sequences themselves are seeded by `/investors/capture`
(FR-3b in this PR -- the capture endpoint now creates a row in
the `sequences` table on first capture). The daily build loop +
follow-up draft generation are FR-4 / FR-5.

State transitions:
  active -> stopped {reason: reply | pipeline | manual |
                     max_touches | fund_news | user}
  active -> completed (when current_touch reaches max_touches
                       AND the last touch is sent)

`/skip` advances next_touch_due_at by N days WITHOUT consuming a
touch -- useful when the operator sees a fund just announced and
wants to defer the next nudge a week.
"""
from __future__ import annotations

import datetime as _dt
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.db import partners, sequences, upsert
from web.deps import _engine_and_ws, require_auth


_VALID_STOP_REASONS = {
    "reply", "pipeline", "manual", "max_touches",
    "fund_news", "user",
}


# ---------- schemas ----------

class SequenceView(BaseModel):
    sequence_id: str
    partner_id: str
    thread_id: str | None = None
    state: str  # active | stopped | completed
    stopped_reason: str | None = None
    current_touch: int
    next_touch_due_at: str | None = None
    created_at: str
    updated_at: str


class StopBody(BaseModel):
    reason: str = Field(
        default="user",
        description=(
            "Why the sequence stopped. Whitelisted server-side: "
            "reply | pipeline | manual | max_touches | "
            "fund_news | user."
        ),
    )


class SkipBody(BaseModel):
    days: int = Field(
        default=3, ge=1, le=365,
        description=(
            "Days to push next_touch_due_at forward. Doesn't "
            "consume a touch -- the operator's just delaying the "
            "next nudge."
        ),
    )


def _row_to_view(row) -> SequenceView:
    return SequenceView(
        sequence_id=row.sequence_id,
        partner_id=row.partner_id,
        thread_id=row.thread_id,
        state=row.state,
        stopped_reason=row.stopped_reason,
        current_touch=int(row.current_touch),
        next_touch_due_at=(
            row.next_touch_due_at.isoformat()
            if row.next_touch_due_at else None
        ),
        created_at=(
            row.created_at.isoformat() if row.created_at else ""
        ),
        updated_at=(
            row.updated_at.isoformat() if row.updated_at else ""
        ),
    )


# ---------- helpers used by /investors/capture ----------

def seed_sequence_for_partner(
    conn, *, partner_id: str, thread_id: str | None = None,
) -> str:
    """Insert a fresh active sequence row for a partner. Called
    by /investors/capture in FR-3b so each captured investor has
    a seeded sequence ready for the daily follow-up build loop
    (FR-4) to advance.

    Idempotent: if the partner already has a sequence, return
    that existing sequence_id and don't create a duplicate.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    existing = conn.execute(
        select(sequences.c.sequence_id).where(
            sequences.c.partner_id == partner_id,
        )
    ).first()
    if existing is not None:
        return existing.sequence_id

    seq_id = "seq_" + secrets.token_hex(8)
    conn.execute(sequences.insert().values(
        sequence_id=seq_id,
        partner_id=partner_id,
        thread_id=thread_id,
        state="active",
        current_touch=1,
        next_touch_due_at=None,
        created_at=now,
        updated_at=now,
    ))
    return seq_id


router = APIRouter(tags=["sequences"])


@router.get(
    "/sequences/{partner_id}",
    response_model=SequenceView,
    summary="Read the active sequence for a partner",
)
def get_sequence(
    partner_id: str,
    _auth: None = Depends(require_auth),
) -> SequenceView:
    """404 if the partner doesn't have a sequence yet (either the
    partner doesn't exist, or this is a Stage-2-enriched partner
    that never went through /investors/capture). Frontend treats
    404 as "no sequence yet, nothing to render"."""
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        row = conn.execute(
            select(sequences).where(
                sequences.c.partner_id == partner_id,
            )
        ).first()
    if row is None:
        raise HTTPException(
            404, f"no sequence on file for partner_id={partner_id}",
        )
    return _row_to_view(row)


@router.post(
    "/sequences/{sequence_id}/stop",
    response_model=SequenceView,
    summary=(
        "Stop a sequence (replies / pipeline / manual / "
        "max_touches / fund_news / user)"
    ),
)
def stop_sequence(
    sequence_id: str,
    body: StopBody,
    _auth: None = Depends(require_auth),
) -> SequenceView:
    """Operator-initiated stop is the common case (reason='user').
    The daily build loop + auto-stop hooks (FR-4 + B3 reconcile +
    B6 CRM pipeline) call this with the appropriate reason.

    Idempotent: stopping an already-stopped sequence returns the
    existing row without mutation.
    """
    reason = (body.reason or "user").strip().lower()
    if reason not in _VALID_STOP_REASONS:
        raise HTTPException(
            422,
            f"reason must be one of {sorted(_VALID_STOP_REASONS)}; "
            f"got {body.reason!r}",
        )
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        row = conn.execute(
            select(sequences).where(
                sequences.c.sequence_id == sequence_id,
            )
        ).first()
        if row is None:
            raise HTTPException(
                404, f"unknown sequence_id: {sequence_id}",
            )
        if row.state != "stopped":
            conn.execute(
                sequences.update()
                .where(sequences.c.sequence_id == sequence_id)
                .values(
                    state="stopped",
                    stopped_reason=reason,
                    updated_at=now,
                )
            )
        row = conn.execute(
            select(sequences).where(
                sequences.c.sequence_id == sequence_id,
            )
        ).first()
    return _row_to_view(row)


@router.post(
    "/sequences/{sequence_id}/skip",
    response_model=SequenceView,
    summary=(
        "Push next_touch_due_at forward by N days without "
        "consuming a touch"
    ),
)
def skip_sequence(
    sequence_id: str,
    body: SkipBody,
    _auth: None = Depends(require_auth),
) -> SequenceView:
    """Used when the operator sees a fresh fund news / earnings
    item and wants to delay the next nudge by a few days. Doesn't
    advance current_touch -- just shifts the schedule.

    422 if the sequence is stopped (resuming requires a separate
    flow we haven't designed yet).
    """
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        row = conn.execute(
            select(sequences).where(
                sequences.c.sequence_id == sequence_id,
            )
        ).first()
        if row is None:
            raise HTTPException(
                404, f"unknown sequence_id: {sequence_id}",
            )
        if row.state != "active":
            raise HTTPException(
                422,
                f"cannot skip a {row.state!r} sequence; only "
                f"active sequences can be deferred",
            )
        # If next_touch_due_at is unset (initial state pre-FR-4),
        # interpret 'skip' as "schedule next touch for now+N days".
        base = row.next_touch_due_at or now.replace(tzinfo=None)
        new_due = base + _dt.timedelta(days=body.days)
        conn.execute(
            sequences.update()
            .where(sequences.c.sequence_id == sequence_id)
            .values(next_touch_due_at=new_due, updated_at=now)
        )
        row = conn.execute(
            select(sequences).where(
                sequences.c.sequence_id == sequence_id,
            )
        ).first()
    return _row_to_view(row)
