"""Unit + integration tests for core/relationships.py + Slice 7 wiring."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6

from core.relationships import (
    CONTACTED_COOLDOWN_DAYS,
    PASSED_COOLDOWN_DAYS,
    STATE_ACTIVE_CONVERSATION,
    STATE_CONTACTED,
    STATE_DO_NOT_CONTACT,
    STATE_INVESTED,
    STATE_NONE,
    STATE_PASSED,
    state_from_outcome_event,
    suppress_outreach,
)


_NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


# ----- suppression rules -----


def test_none_state_no_suppression() -> None:
    s = suppress_outreach(
        relationship_status=STATE_NONE,
        last_contacted_at=None, last_reply_at=None,
        do_not_contact=False, now=_NOW,
    )
    assert s.suppressed is False


def test_do_not_contact_always_suppresses() -> None:
    s = suppress_outreach(
        relationship_status=STATE_NONE,
        last_contacted_at=None, last_reply_at=None,
        do_not_contact=True, now=_NOW,
    )
    assert s.suppressed is True
    assert "do_not_contact" in (s.reason or "")


def test_active_conversation_suppresses() -> None:
    s = suppress_outreach(
        relationship_status=STATE_ACTIVE_CONVERSATION,
        last_contacted_at=None, last_reply_at=None,
        do_not_contact=False, now=_NOW,
    )
    assert s.suppressed is True
    assert "active_conversation" in (s.reason or "")


def test_invested_suppresses_permanently() -> None:
    s = suppress_outreach(
        relationship_status=STATE_INVESTED,
        last_contacted_at=None, last_reply_at=None,
        do_not_contact=False, now=_NOW,
    )
    assert s.suppressed is True


def test_passed_within_cooldown_suppresses() -> None:
    recent = _NOW - timedelta(days=30)
    s = suppress_outreach(
        relationship_status=STATE_PASSED,
        last_contacted_at=None, last_reply_at=recent,
        do_not_contact=False, now=_NOW,
        passed_cooldown_days=PASSED_COOLDOWN_DAYS,
    )
    assert s.suppressed is True
    assert "30d ago" in (s.reason or "") or "cooldown" in (s.reason or "")


def test_passed_after_cooldown_allows() -> None:
    long_ago = _NOW - timedelta(days=PASSED_COOLDOWN_DAYS + 30)
    s = suppress_outreach(
        relationship_status=STATE_PASSED,
        last_contacted_at=None, last_reply_at=long_ago,
        do_not_contact=False, now=_NOW,
        passed_cooldown_days=PASSED_COOLDOWN_DAYS,
    )
    assert s.suppressed is False


def test_passed_with_missing_reply_timestamp_suppresses_by_default() -> None:
    """Defensive: a `passed` state with no reply timestamp shouldn't
    silently unlock outreach. Suppress until the operator adds
    context."""
    s = suppress_outreach(
        relationship_status=STATE_PASSED,
        last_contacted_at=None, last_reply_at=None,
        do_not_contact=False, now=_NOW,
    )
    assert s.suppressed is True


def test_contacted_within_cooldown_suppresses() -> None:
    recent = _NOW - timedelta(days=5)
    s = suppress_outreach(
        relationship_status=STATE_CONTACTED,
        last_contacted_at=recent, last_reply_at=None,
        do_not_contact=False, now=_NOW,
        contacted_cooldown_days=CONTACTED_COOLDOWN_DAYS,
    )
    assert s.suppressed is True


def test_contacted_after_cooldown_allows() -> None:
    long_ago = _NOW - timedelta(days=CONTACTED_COOLDOWN_DAYS + 5)
    s = suppress_outreach(
        relationship_status=STATE_CONTACTED,
        last_contacted_at=long_ago, last_reply_at=None,
        do_not_contact=False, now=_NOW,
        contacted_cooldown_days=CONTACTED_COOLDOWN_DAYS,
    )
    assert s.suppressed is False


def test_future_timestamps_treated_as_no_signal() -> None:
    """A future timestamp (clock skew) shouldn't be 'recent' -- the
    days-ago calculation returns None and we either fall through or
    suppress conservatively depending on the state."""
    future = _NOW + timedelta(days=30)
    s = suppress_outreach(
        relationship_status=STATE_CONTACTED,
        last_contacted_at=future, last_reply_at=None,
        do_not_contact=False, now=_NOW,
    )
    # State is CONTACTED but timestamp is unusable; permissive path.
    assert s.suppressed is False


# ----- outcome -> relationship state mapping -----


def test_outcome_passed_maps_to_passed() -> None:
    s = state_from_outcome_event(
        outreach_status="replied", reply_type="passed_not_a_fit",
        meeting_booked=False, meeting_outcome=None,
    )
    assert s == STATE_PASSED


def test_outcome_meeting_booked_maps_to_active() -> None:
    s = state_from_outcome_event(
        outreach_status="meeting_booked", reply_type=None,
        meeting_booked=True, meeting_outcome=None,
    )
    assert s == STATE_ACTIVE_CONVERSATION


def test_outcome_invested_reply_maps_to_invested() -> None:
    s = state_from_outcome_event(
        outreach_status="replied", reply_type="invested",
        meeting_booked=False, meeting_outcome=None,
    )
    assert s == STATE_INVESTED


def test_outcome_sent_maps_to_contacted() -> None:
    s = state_from_outcome_event(
        outreach_status="sent", reply_type=None,
        meeting_booked=False, meeting_outcome=None,
    )
    assert s == STATE_CONTACTED


def test_outcome_no_signal_returns_none() -> None:
    """An outcome event with no decipherable state shouldn't
    overwrite the partner's existing relationship_status."""
    s = state_from_outcome_event(
        outreach_status=None, reply_type=None,
        meeting_booked=False, meeting_outcome=None,
    )
    assert s is None


# ----- integration: outcome hydration -----


def test_outcome_persistence_hydrates_partner_relationship():
    """When an OutcomeEvent inserts, the partner's
    relationship_status + last_* timestamps update in the same
    transaction so suppression sees fresh state immediately."""
    from core.db import funds, get_engine, partners
    from core.outcomes.events import OutcomeEvent
    from core.outcomes.persistence import persist_outcome_event

    with tempfile.TemporaryDirectory() as tmpdir:
        engine = get_engine(f"sqlite:///{Path(tmpdir) / 'test.db'}")
        with engine.begin() as conn:
            conn.execute(funds.insert().values(
                fund_id="f1", name="Test", domain="t.example", is_active=True,
            ))
            conn.execute(partners.insert().values(
                partner_id="p1", fund_id="f1", name="Test Partner",
                relationship_status=STATE_NONE,
            ))
        from datetime import date as _date
        event = OutcomeEvent(
            partner_id="p1",
            outreach_status="replied",
            reply_type="passed_not_a_fit",
            meeting_booked=False,
            meeting_date=None,
            meeting_outcome=None,
            source="manual",
            external_event_id="manual:test1",
            observed_at=_NOW,
        )
        outcome_id = persist_outcome_event(engine, event)
        assert outcome_id is not None
        from sqlalchemy import select as _select
        with engine.begin() as conn:
            row = conn.execute(
                _select(
                    partners.c.relationship_status,
                    partners.c.last_reply_at,
                    partners.c.outcome_source,
                    partners.c.last_outcome,
                ).where(partners.c.partner_id == "p1"),
            ).first()
        assert row[0] == STATE_PASSED
        assert row[1] is not None
        assert row[2] == "manual"
        assert row[3] == "passed_not_a_fit"


# ----- integration: set_relationship CLI -----


def test_set_relationship_cli_updates_partner():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        pid = c.execute("select partner_id from partners limit 1").fetchone()[0]
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_relationship.py"),
             "--workspace", ws, "--partner-id", pid,
             "--status", "passed", "--notes", "I spoke with them; not a fit"],
            capture_output=True, text=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        c = sqlite3.connect(db)
        row = c.execute(
            "select relationship_status, owner_notes, outcome_source "
            "from partners where partner_id = ?", (pid,),
        ).fetchone()
        c.close()
        assert row[0] == "passed"
        assert "not a fit" in (row[1] or "")
        assert row[2] == "manual"


def test_set_relationship_stales_approved_drafts():
    """Setting a suppressive relationship state on a partner with an
    approved draft flips that draft to stale_after_approval (Slice 1
    invalidation rule: relationship_changed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _run(
            "07_generate_emails.py", "--workspace", ws,
            "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
        )
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended = 1 limit 1"
        ).fetchone()
        c.execute(
            "update email_drafts set approval_status='approved_to_send' "
            "where draft_id = ?", (draft_id,),
        )
        c.execute(
            "update partners set email='priya@apollo.example' "
            "where partner_id = ?", (pid,),
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_relationship.py"),
             "--workspace", ws, "--partner-id", pid,
             "--status", "active_conversation",
             "--notes", "exchange in progress"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "stale_after_approval" in res.stdout

        c = sqlite3.connect(db)
        status, latest_event = c.execute(
            "select e.approval_status, a.event_type "
            "from email_drafts e "
            "join draft_approvals a on a.draft_id = e.draft_id "
            "where e.draft_id = ? "
            "order by a.event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert status == "stale_after_approval"
        assert latest_event == "stale_after_approval"
