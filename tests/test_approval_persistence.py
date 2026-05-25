"""Unit tests for core/approval/persistence.py (Slice 1)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.approval import persistence as ap
from core.approval.state_machine import (
    InvalidApprovalTransition,
    STATE_APPROVED_TO_SEND,
    STATE_NEEDS_REVIEW,
    STATE_REJECTED,
    STATE_SENT,
    STATE_STALE_AFTER_APPROVAL,
    TRIGGER_BODY_REGENERATED,
)
from core.db import (
    draft_approvals as ev_table,
    email_drafts,
    funds,
    get_engine,
    partners,
)


@pytest.fixture
def engine(tmp_path: Path):
    """Isolated SQLite with a fund + partner + one draft seeded so
    transition() has something to operate on."""
    eng = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="Test Fund",
            domain="t.example", is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="p1", fund_id="f1", name="Test Partner",
        ))
        # Note: draft is inserted WITHOUT approval_status / draft_hash;
        # seed_draft() is responsible for setting both.
        conn.execute(email_drafts.insert().values(
            draft_id=10, partner_id="p1", batch_id="b1",
            subject="hello", body="world",
            is_recommended=True, generated_at=datetime.now(timezone.utc),
        ))
    return eng


# ----- compute_draft_hash -----


def test_draft_hash_is_deterministic() -> None:
    assert ap.compute_draft_hash("a", "b") == ap.compute_draft_hash("a", "b")


def test_draft_hash_subject_change_flips_hash() -> None:
    a = ap.compute_draft_hash("subject A", "body")
    b = ap.compute_draft_hash("subject B", "body")
    assert a != b


def test_draft_hash_body_change_flips_hash() -> None:
    a = ap.compute_draft_hash("subj", "body A")
    b = ap.compute_draft_hash("subj", "body B")
    assert a != b


def test_draft_hash_none_safe() -> None:
    """Stage 7 may persist drafts with NULL subject in edge cases;
    hashing should not crash."""
    h = ap.compute_draft_hash(None, None)
    assert h and len(h) == 64


def test_draft_hash_treats_empty_and_none_as_equivalent() -> None:
    """Documented behavior: canonical form normalizes None -> "".
    A draft with subject="" hashes the same as subject=None. Pin the
    invariant so a future None-vs-empty distinction is an explicit
    breaking change, not silent."""
    assert ap.compute_draft_hash("", "") == ap.compute_draft_hash(None, None)


# ----- seed_draft -----


def test_seed_draft_sets_pointer_and_writes_event(engine) -> None:
    ap.seed_draft(
        engine, draft_id=10, partner_id="p1",
        draft_hash="abc", actor="system",
    )
    assert ap.latest_state(engine, 10) == STATE_NEEDS_REVIEW
    events = ap.list_events(engine, 10)
    assert len(events) == 1
    assert events[0].event_type == STATE_NEEDS_REVIEW
    assert events[0].actor == "system"
    assert events[0].draft_hash == "abc"


def test_seed_draft_is_idempotent(engine) -> None:
    """Stage 7 re-runs on the same draft_id must not duplicate seed
    events. Second call is a no-op."""
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="abc")
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="abc")
    events = ap.list_events(engine, 10)
    assert len(events) == 1


def test_seed_draft_updates_email_drafts_columns(engine) -> None:
    ap.seed_draft(
        engine, draft_id=10, partner_id="p1", draft_hash="h_seed",
    )
    with engine.begin() as conn:
        row = conn.execute(
            select(
                email_drafts.c.approval_status,
                email_drafts.c.draft_hash,
            ).where(email_drafts.c.draft_id == 10)
        ).first()
    assert row.approval_status == STATE_NEEDS_REVIEW
    assert row.draft_hash == "h_seed"


# ----- transition -----


def test_human_approve_writes_event_and_updates_pointer(engine) -> None:
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley",
               notes="strong hook")
    assert ap.latest_state(engine, 10) == STATE_APPROVED_TO_SEND
    events = ap.list_events(engine, 10)
    assert [e.event_type for e in events] == [
        STATE_NEEDS_REVIEW, STATE_APPROVED_TO_SEND,
    ]
    assert events[1].actor == "ashley"
    assert events[1].notes == "strong hook"
    # The approval event preserves the body hash so we can later
    # prove THIS exact body was approved.
    assert events[1].draft_hash == "h1"


def test_system_cannot_approve(engine) -> None:
    """The whole point of the gate: a cron script accidentally
    calling transition(to='approved_to_send', source='system') must
    raise."""
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    with pytest.raises(InvalidApprovalTransition):
        ap.transition(
            engine, draft_id=10, partner_id="p1",
            to_state=STATE_APPROVED_TO_SEND,
            actor="cron", source="system",
        )


def test_reject_then_re_queue_walks_full_cycle(engine) -> None:
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    ap.reject(engine, draft_id=10, partner_id="p1",
              actor="ashley", notes="off-base")
    assert ap.latest_state(engine, 10) == STATE_REJECTED
    # Operator un-rejects: rejected -> needs_review (human edge).
    ap.transition(
        engine, draft_id=10, partner_id="p1",
        to_state=STATE_NEEDS_REVIEW, actor="ashley", source="human",
    )
    assert ap.latest_state(engine, 10) == STATE_NEEDS_REVIEW


def test_mark_stale_records_trigger_in_notes(engine) -> None:
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley")
    ap.mark_stale(
        engine, draft_id=10, partner_id="p1",
        trigger=TRIGGER_BODY_REGENERATED,
    )
    events = ap.list_events(engine, 10)
    stale = events[-1]
    assert stale.event_type == STATE_STALE_AFTER_APPROVAL
    assert stale.actor == "system"
    assert TRIGGER_BODY_REGENERATED in (stale.notes or "")


def test_stale_then_re_approve_is_human_action(engine) -> None:
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley")
    ap.mark_stale(
        engine, draft_id=10, partner_id="p1",
        trigger=TRIGGER_BODY_REGENERATED,
    )
    # Operator re-approves directly (stale -> approved_to_send, human).
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley",
               notes="reviewed regen; still good")
    assert ap.latest_state(engine, 10) == STATE_APPROVED_TO_SEND


def test_mark_sent_is_terminal(engine) -> None:
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h1")
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley")
    ap.mark_sent(engine, draft_id=10, partner_id="p1")
    assert ap.latest_state(engine, 10) == STATE_SENT
    # Any further transition raises.
    with pytest.raises(InvalidApprovalTransition):
        ap.reject(engine, draft_id=10, partner_id="p1", actor="ashley")


def test_transition_on_unknown_draft_raises(engine) -> None:
    with pytest.raises(ValueError):
        ap.approve(engine, draft_id=999, partner_id="p1", actor="x")


def test_transition_passes_explicit_hash_into_event_and_pointer(engine) -> None:
    """When stale-after-approval is triggered by a regeneration with
    a NEW hash, the event + pointer must reflect the new hash so
    the audit shows what the new body was."""
    ap.seed_draft(engine, draft_id=10, partner_id="p1", draft_hash="h_old")
    ap.approve(engine, draft_id=10, partner_id="p1", actor="ashley")
    ap.transition(
        engine, draft_id=10, partner_id="p1",
        to_state=STATE_STALE_AFTER_APPROVAL,
        actor="system", source="system",
        draft_hash="h_new",
        notes="body_regenerated",
    )
    events = ap.list_events(engine, 10)
    assert events[-1].draft_hash == "h_new"
    with engine.begin() as conn:
        pointer_hash = conn.execute(
            select(email_drafts.c.draft_hash).where(
                email_drafts.c.draft_id == 10,
            )
        ).scalar()
    assert pointer_hash == "h_new"


# ----- pending_review / approved_for_send -----


def test_pending_review_lists_needs_review_and_stale_only(engine) -> None:
    """The review-queue feed includes both fresh + stale; excludes
    approved (already in the send queue) and rejected (operator
    already decided)."""
    # Insert 4 drafts in 4 states.
    other_drafts = [
        (11, STATE_APPROVED_TO_SEND),
        (12, STATE_REJECTED),
        (13, STATE_NEEDS_REVIEW),
        (14, STATE_STALE_AFTER_APPROVAL),
    ]
    with engine.begin() as conn:
        for did, status in other_drafts:
            conn.execute(email_drafts.insert().values(
                draft_id=did, partner_id="p1", batch_id="b1",
                subject="s", body="b",
                is_recommended=True,
                approval_status=status,
                generated_at=datetime.now(timezone.utc),
            ))
    rows = ap.pending_review(engine)
    review_ids = {r.draft_id for r in rows}
    # Fixture-seeded draft 10 also defaulted to needs_review on insert.
    assert review_ids == {10, 13, 14}


def test_approved_for_send_includes_only_approved_to_send(engine) -> None:
    """The single canonical send-queue read. Must NOT include
    stale_after_approval or rejected, even if they were previously
    approved."""
    others = [
        (20, STATE_APPROVED_TO_SEND),
        (21, STATE_STALE_AFTER_APPROVAL),
        (22, STATE_REJECTED),
        (23, STATE_SENT),
        (24, STATE_APPROVED_TO_SEND),
    ]
    with engine.begin() as conn:
        for did, status in others:
            conn.execute(email_drafts.insert().values(
                draft_id=did, partner_id="p1", batch_id="b1",
                subject="s", body="b",
                is_recommended=True,
                approval_status=status,
                generated_at=datetime.now(timezone.utc),
            ))
    rows = ap.approved_for_send(engine)
    ids = {r.draft_id for r in rows}
    assert ids == {20, 24}
