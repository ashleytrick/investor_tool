"""Approval-workflow persistence (Slice 1).

Writes draft_approvals event rows + keeps the email_drafts.approval_status
pointer in sync. Every transition lands in ONE transaction so the
pointer never disagrees with the event log.

Three entry points:

  - compute_draft_hash(subject, body) -> str
      Canonical sha256 of (subject + body). Stored on every event
      so the operator can prove "this exact body was approved" and
      so the stale-after-approval detector can compare hashes after
      a regeneration.

  - seed_draft(engine, draft_id, partner_id, *, draft_hash, actor,
               notes) -> None
      Called by Stage 7 on draft insert. Writes the (None ->
      needs_review) event and sets email_drafts.approval_status.

  - transition(engine, draft_id, partner_id, to_state, *, actor,
               source, draft_hash, notes) -> None
      Generic edge writer. Validates via state_machine, then commits
      the event + pointer update atomically.

Helper queries:

  - latest_state(engine, draft_id) -> str | None
  - list_events(engine, draft_id) -> list[Row]
  - pending_review(engine) -> list[Row]
      The review queue feed. Returns drafts whose approval_status
      is in REVIEWABLE_STATES.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update

from core.approval.state_machine import (
    APPROVED_STATES,
    REVIEWABLE_STATES,
    STATE_NEEDS_REVIEW,
    assert_can_transition,
)
from core.db import draft_approvals, email_drafts


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_draft_hash(subject: str | None, body: str | None) -> str:
    """sha256(subject + '\\n' + body) hex digest. Canonical form so the
    same draft always hashes the same and a single-char change in
    either field flips the hash."""
    canonical = (subject or "") + "\n" + (body or "")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest_state(engine: Any, draft_id: int) -> str | None:
    """Read the current approval_status pointer. Returns None when
    the draft doesn't exist (caller decides whether to raise)."""
    with engine.begin() as conn:
        row = conn.execute(
            select(email_drafts.c.approval_status).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).first()
    if row is None:
        return None
    return row.approval_status


def _seed_draft_using_conn(
    conn: Any,
    *,
    draft_id: int,
    partner_id: str,
    draft_hash: str,
    actor: str,
    notes: str | None,
) -> None:
    """Inner: seed using an already-open connection. Used by callers
    that are inside an outer transaction (Stage 7) so SQLite doesn't
    deadlock on a nested engine.begin()."""
    assert_can_transition(None, STATE_NEEDS_REVIEW, source="system")
    existing = conn.execute(
        select(draft_approvals.c.event_id).where(
            draft_approvals.c.draft_id == draft_id,
        ).limit(1)
    ).first()
    if existing is not None:
        return
    now = _now()
    conn.execute(draft_approvals.insert().values(
        draft_id=draft_id,
        partner_id=partner_id,
        event_type=STATE_NEEDS_REVIEW,
        actor=actor,
        at=now,
        draft_hash=draft_hash,
        notes=notes,
    ))
    conn.execute(
        update(email_drafts)
        .where(email_drafts.c.draft_id == draft_id)
        .values(
            approval_status=STATE_NEEDS_REVIEW,
            draft_hash=draft_hash,
        )
    )


def seed_draft(
    engine_or_conn: Any,
    *,
    draft_id: int,
    partner_id: str,
    draft_hash: str,
    actor: str = "system",
    notes: str | None = None,
) -> None:
    """Initial seed event for a newly-inserted draft. Stage 7 calls
    this immediately after inserting an email_drafts row. Idempotent:
    if the draft already has a needs_review event, this is a no-op
    (lets Stage 7's re-runs land cleanly without duplicate events).

    Accepts either an Engine (opens its own transaction) or an
    existing Connection (reuses the caller's transaction -- required
    when the caller is already inside engine.begin() to avoid SQLite
    deadlocking on nested write transactions).
    """
    # Duck-type: a Connection has .execute and no .begin(); an Engine
    # has both. The simplest correct check is `hasattr(x, 'begin')`
    # AND `callable(x.begin)` AND `not x.in_transaction()`. Use a
    # cheaper check that works for both SQLAlchemy 1.4 + 2.0: try
    # treating it as a connection first.
    if hasattr(engine_or_conn, "execute") and not hasattr(
        engine_or_conn, "connect",
    ):
        _seed_draft_using_conn(
            engine_or_conn, draft_id=draft_id, partner_id=partner_id,
            draft_hash=draft_hash, actor=actor, notes=notes,
        )
        return
    with engine_or_conn.begin() as conn:
        _seed_draft_using_conn(
            conn, draft_id=draft_id, partner_id=partner_id,
            draft_hash=draft_hash, actor=actor, notes=notes,
        )


def transition(
    engine: Any,
    *,
    draft_id: int,
    partner_id: str,
    to_state: str,
    actor: str,
    source: str,
    draft_hash: str | None = None,
    notes: str | None = None,
    overridden_blockers: list[str] | None = None,
) -> None:
    """Generic edge writer. Validates the (current -> to_state) edge
    against the state-machine table, then writes the event + updates
    the pointer in one transaction.

    `source` must match what the transition table expects ('human'
    for operator-driven edges, 'system' for auto-generated). The
    state machine raises InvalidApprovalTransition on mismatch.

    `draft_hash` defaults to the current pointer's hash on
    email_drafts (so an approve event records exactly what was
    approved). Pass an explicit hash when the transition's whole
    point is detecting a hash change (e.g. stale_after_approval
    triggered by a regeneration).
    """
    with engine.begin() as conn:
        row = conn.execute(
            select(
                email_drafts.c.approval_status,
                email_drafts.c.draft_hash,
            ).where(email_drafts.c.draft_id == draft_id)
        ).first()
        if row is None:
            raise ValueError(
                f"transition called on unknown draft_id={draft_id!r}",
            )
        current = row.approval_status
        # Validate edge BEFORE writing anything.
        assert_can_transition(current, to_state, source=source)
        effective_hash = draft_hash if draft_hash is not None else row.draft_hash
        conn.execute(draft_approvals.insert().values(
            draft_id=draft_id,
            partner_id=partner_id,
            event_type=to_state,
            actor=actor,
            at=_now(),
            draft_hash=effective_hash,
            notes=notes,
            overridden_blockers=(
                json.dumps(list(overridden_blockers))
                if overridden_blockers else None
            ),
        ))
        conn.execute(
            update(email_drafts)
            .where(email_drafts.c.draft_id == draft_id)
            .values(
                approval_status=to_state,
                # Refresh the pointer's hash too when an explicit
                # hash was passed (regeneration / human edit cases).
                draft_hash=effective_hash,
            )
        )


def list_events(engine: Any, draft_id: int) -> list[Any]:
    """All events for a draft in chronological order. Used by the
    operator audit + by tests asserting state-machine behavior."""
    with engine.begin() as conn:
        return list(conn.execute(
            select(draft_approvals)
            .where(draft_approvals.c.draft_id == draft_id)
            .order_by(draft_approvals.c.event_id)
        ))


def pending_review(engine: Any) -> list[Any]:
    """Drafts whose pointer is in REVIEWABLE_STATES, ordered by
    partner_id then draft_id. Drives `list_pending_review.py` +
    the future review-queue UI."""
    with engine.begin() as conn:
        return list(conn.execute(
            select(email_drafts)
            .where(email_drafts.c.approval_status.in_(REVIEWABLE_STATES))
            .order_by(email_drafts.c.partner_id, email_drafts.c.draft_id)
        ))


def approved_for_send(engine: Any) -> list[Any]:
    """Drafts cleared for Gmail / Attio / send_queue.csv. The single
    canonical read for any 'send this' consumer -- never branch on
    approval_status string literals directly."""
    with engine.begin() as conn:
        return list(conn.execute(
            select(email_drafts)
            .where(email_drafts.c.approval_status.in_(APPROVED_STATES))
            .order_by(email_drafts.c.draft_id)
        ))


# Convenience helpers wrapping `transition` for the common human
# actions. Keep the verbose signature available for tests + future
# UI; the CLI uses these.


def approve(
    engine: Any, *, draft_id: int, partner_id: str,
    actor: str, notes: str | None = None,
    overridden_blockers: list[str] | None = None,
) -> None:
    """Human approval. Validates the transition + writes the event.

    `overridden_blockers` is the list of SOFT blockers the operator
    explicitly acknowledged via `--override-blockers`. Persisted on
    the event so downstream gate re-checks (Gmail, Attio, send-queue
    export) can honor the override.
    """
    transition(
        engine, draft_id=draft_id, partner_id=partner_id,
        to_state="approved_to_send", actor=actor, source="human",
        notes=notes, overridden_blockers=overridden_blockers,
    )


def reject(
    engine: Any, *, draft_id: int, partner_id: str,
    actor: str, notes: str | None = None,
) -> None:
    """Human rejection."""
    transition(
        engine, draft_id=draft_id, partner_id=partner_id,
        to_state="rejected", actor=actor, source="human",
        notes=notes,
    )


def mark_stale(
    engine: Any, *, draft_id: int, partner_id: str,
    trigger: str, notes: str | None = None,
) -> None:
    """System-driven invalidation. `trigger` should be one of the
    TRIGGER_* constants from state_machine; appended to notes for the
    audit trail."""
    full_notes = trigger if notes is None else f"{trigger}: {notes}"
    transition(
        engine, draft_id=draft_id, partner_id=partner_id,
        to_state="stale_after_approval", actor="system", source="system",
        notes=full_notes,
    )


def mark_sent(
    engine: Any, *, draft_id: int, partner_id: str,
    actor: str = "system", notes: str | None = None,
) -> None:
    """Terminal transition after Gmail / Attio confirms delivery."""
    transition(
        engine, draft_id=draft_id, partner_id=partner_id,
        to_state="sent", actor=actor, source="system",
        notes=notes,
    )
