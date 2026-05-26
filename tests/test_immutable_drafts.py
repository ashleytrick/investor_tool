"""Tests for Slice 17 immutable draft history."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def _stage7(ws: str) -> None:
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )


def _force_hash_drift(db: Path) -> None:
    """Stage 7's identical-hash re-run is now a no-op (PR A fix). The
    stub LLM is deterministic, so any test that wants to assert the
    supersede + stale path runs MUST drift the prior recommended
    rows' draft_hash to a sentinel before re-running Stage 7. That
    flips the no-op check to "real regeneration" and the supersede
    UPDATE + mark_stale_using_conn fire as before."""
    c = sqlite3.connect(db)
    c.execute(
        "update email_drafts set draft_hash='_force_drift_' "
        "where is_recommended=1 and superseded_at is null"
    )
    c.commit()
    c.close()


def test_stage7_rerun_supersedes_prior_drafts_does_not_delete():
    """A second Stage 7 run on the same workspace must NOT remove the
    prior drafts; it should mark them superseded_at + clear
    is_recommended + insert new rows with version+1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        _stage7(ws)
        c = sqlite3.connect(db)
        first_count = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        first_live = c.execute(
            "select count(*) from email_drafts where superseded_at is null"
        ).fetchone()[0]
        c.close()
        assert first_count > 0
        assert first_count == first_live, (
            "first run: every draft should be live"
        )

        # Re-run Stage 7 -- prior drafts should be superseded, NOT
        # deleted. Total row count grows; live count stays the same.
        # Force hash drift first so the no-op check doesn't short-
        # circuit (stub LLM is deterministic).
        _force_hash_drift(db)
        _stage7(ws)
        c = sqlite3.connect(db)
        second_count = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        second_live = c.execute(
            "select count(*) from email_drafts where superseded_at is null"
        ).fetchone()[0]
        superseded_count = c.execute(
            "select count(*) from email_drafts where superseded_at is not null"
        ).fetchone()[0]
        # Every superseded row has is_recommended=False (so latest-rec
        # readers stay unambiguous).
        bad_rec = c.execute(
            "select count(*) from email_drafts "
            "where superseded_at is not null and is_recommended=1"
        ).fetchone()[0]
        # Every superseded row's version is < the live row's version
        # for the same partner.
        version_ordering_violations = c.execute(
            """select count(*) from email_drafts a
            join email_drafts b on a.partner_id = b.partner_id
            where a.superseded_at is not null
              and b.superseded_at is null
              and a.version >= b.version"""
        ).fetchone()[0]
        c.close()
        assert second_count == first_count * 2, (
            f"history was deleted: first={first_count} second={second_count}"
        )
        assert second_live == first_live
        assert superseded_count == first_count
        assert bad_rec == 0, (
            "superseded rows must clear is_recommended"
        )
        assert version_ordering_violations == 0


def test_approved_draft_supersede_runs_state_machine_stale():
    """When Stage 7 supersedes a draft that was approved_to_send, the
    approval state machine must record a stale_after_approval event
    + flip the pointer."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        _stage7(ws)
        # Pick a recommended draft, prime the partner for approval, approve.
        c = sqlite3.connect(db)
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            check=True, capture_output=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        # Sanity: approval landed.
        c = sqlite3.connect(db)
        status_before = c.execute(
            "select approval_status from email_drafts where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        assert status_before == "approved_to_send"

        # Re-run Stage 7 -- the approved draft gets superseded, which
        # must trigger the body_regenerated state-machine event.
        # Force hash drift so the no-op check fires the supersede
        # path (deterministic stub LLM otherwise no-ops the rerun).
        _force_hash_drift(db)
        _stage7(ws)

        c = sqlite3.connect(db)
        status_after = c.execute(
            "select approval_status from email_drafts where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        latest_event = c.execute(
            "select event_type, notes from draft_approvals "
            "where draft_id=? order by event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert status_after == "stale_after_approval", (
            "supersede must run mark_stale via the state machine"
        )
        assert latest_event[0] == "stale_after_approval"
        assert "body_regenerated" in (latest_event[1] or "")


def test_list_draft_history_shows_all_versions():
    """The new CLI surfaces every version + marks live vs superseded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        _stage7(ws)
        _force_hash_drift(db)  # avoid the identical-hash no-op
        _stage7(ws)  # two generations
        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from email_drafts limit 1"
        ).fetchone()[0]
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "list_draft_history.py"),
             "--workspace", ws, "--partner-id", pid, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        import json
        rows = json.loads(res.stdout)
        # Two generations x 2 variants = 4 rows.
        assert len(rows) == 4
        # Exactly half should be live (newer generation) + half
        # superseded.
        live = [r for r in rows if r["superseded_at"] is None]
        super_ = [r for r in rows if r["superseded_at"] is not None]
        assert len(live) == 2
        assert len(super_) == 2
        # Live versions strictly greater than superseded.
        max_super = max(r["version"] for r in super_)
        min_live = min(r["version"] for r in live)
        assert min_live > max_super


def test_stage7_identical_regen_is_noop_and_does_not_stale_approval():
    """Regression: an idempotent Stage 7 re-run on the same workspace
    (deterministic stub LLM -> identical (subject, body) per partner)
    must NOT supersede the prior recommended drafts, must NOT insert
    new email_drafts rows, and must NOT flip an existing
    approved_to_send approval to stale_after_approval.

    Pre-PR-A behavior was: every Stage 7 re-run supersedes the prior
    live rows unconditionally + runs mark_stale on any approved row.
    Operators got "phantom" stale events on a no-op re-run.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        _stage7(ws)
        # Approve a draft so we can prove the approval survives the
        # idempotent re-run.
        c = sqlite3.connect(db)
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        first_total = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        first_super = c.execute(
            "select count(*) from email_drafts where superseded_at is not null"
        ).fetchone()[0]
        first_events = c.execute(
            "select count(*) from draft_approvals where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            check=True, capture_output=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        # Re-run Stage 7 with NO hash drift -- the stub LLM is
        # deterministic so every variant body matches its prior live
        # row exactly. The no-op fix should kick in.
        _stage7(ws)
        c = sqlite3.connect(db)
        # No new rows.
        second_total = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        second_super = c.execute(
            "select count(*) from email_drafts where superseded_at is not null"
        ).fetchone()[0]
        # Approval still approved (no phantom stale event).
        status = c.execute(
            "select approval_status from email_drafts where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        # No new draft_approvals events for that draft beyond the
        # approval we made.
        second_events = c.execute(
            "select count(*) from draft_approvals where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        assert second_total == first_total, (
            f"identical regen inserted new rows: was {first_total}, "
            f"now {second_total}"
        )
        assert second_super == first_super, (
            f"identical regen superseded a row: was {first_super}, "
            f"now {second_super}"
        )
        assert status == "approved_to_send", (
            f"phantom stale: approval flipped to {status} despite "
            f"identical-body re-run"
        )
        # 1 new event from the approve call itself (+1 from initial
        # needs_review seed). No body_regenerated event.
        assert second_events == first_events + 1, (
            f"unexpected new draft_approvals event(s) on idempotent "
            f"re-run: was {first_events}, now {second_events}"
        )


def test_stage7_real_regen_stales_approval_in_same_txn():
    """Mirror of the no-op test: when the new recommended body DOES
    differ from the prior live row (forced via hash drift here), the
    supersede + mark_stale MUST fire, and they must land in the same
    transaction so a crash between can't leave superseded_at IS NOT
    NULL with approval_status='approved_to_send' and no event row.

    We can't directly observe atomicity, but we can assert that on a
    successful run the supersede + stale_after_approval event + the
    pointer flip all show up together for the approved draft.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        _stage7(ws)
        c = sqlite3.connect(db)
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            check=True, capture_output=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        # Force hash drift so the second Stage 7 takes the "real
        # regeneration" path.
        _force_hash_drift(db)
        _stage7(ws)
        c = sqlite3.connect(db)
        status, superseded_at = c.execute(
            "select approval_status, superseded_at from email_drafts "
            "where draft_id=?", (draft_id,),
        ).fetchone()
        latest_event = c.execute(
            "select event_type, notes from draft_approvals "
            "where draft_id=? order by event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert superseded_at is not None, (
            "supersede UPDATE should have stamped superseded_at"
        )
        assert status == "stale_after_approval", (
            f"pointer should flip to stale_after_approval, got {status}"
        )
        assert latest_event[0] == "stale_after_approval", (
            f"latest event should be stale_after_approval, got {latest_event[0]}"
        )
        assert "body_regenerated" in (latest_event[1] or "")


def test_followup_and_deck_supersede_on_stage7_rerun():
    """Slice 17 follow-up (#17): followup_drafts and
    deck_request_responses are now also versioned -- Stage 7 re-runs
    supersede instead of delete, preserving the prior generation for
    audit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        _stage7(ws)
        c = sqlite3.connect(db)
        first_fu = c.execute("select count(*) from followup_drafts").fetchone()[0]
        first_dk = c.execute("select count(*) from deck_request_responses").fetchone()[0]
        c.close()
        assert first_fu > 0
        assert first_dk > 0
        # Force hash drift so the email_drafts no-op check doesn't
        # short-circuit. Followups + decks always supersede when their
        # parent email_drafts is re-generated.
        _force_hash_drift(db)
        _stage7(ws)
        c = sqlite3.connect(db)
        # Live count stays the same, total doubles.
        fu_total = c.execute("select count(*) from followup_drafts").fetchone()[0]
        fu_live = c.execute(
            "select count(*) from followup_drafts where superseded_at is null"
        ).fetchone()[0]
        dk_total = c.execute("select count(*) from deck_request_responses").fetchone()[0]
        dk_live = c.execute(
            "select count(*) from deck_request_responses where superseded_at is null"
        ).fetchone()[0]
        # Version monotonicity: every live row's version > every
        # superseded row's version for the same partner.
        fu_violations = c.execute(
            "select count(*) from followup_drafts a join followup_drafts b "
            "on a.partner_id=b.partner_id "
            "where a.superseded_at is not null and b.superseded_at is null "
            "  and a.version >= b.version"
        ).fetchone()[0]
        c.close()
        assert fu_total == first_fu * 2
        assert fu_live == first_fu
        assert dk_total == first_dk * 2
        assert dk_live == first_dk
        assert fu_violations == 0
