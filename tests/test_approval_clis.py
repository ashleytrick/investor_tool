"""End-to-end tests for the approval-workflow CLIs (Slice 1)."""
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
    """Run Stage 7 end-to-end on a workspace already past Stage 6.
    Need --allow-example-domains because fixture uses .example URLs;
    drafts will land as qa_failed (no email) but the rows still exist
    in email_drafts + draft_approvals so the CLI tests can find them."""
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )


def _draft_id_for_a_partner(db: Path) -> tuple[int, str]:
    c = sqlite3.connect(db)
    row = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where is_recommended = 1 limit 1"
    ).fetchone()
    c.close()
    assert row is not None, "stage 7 should have produced at least one draft"
    return int(row[0]), row[1]


def test_list_pending_review_shows_seeded_drafts():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _stage7(ws)

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "list_pending_review.py"),
             "--workspace", ws],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "pending" in res.stdout.lower()
        # Stage 7 fixture run produces 5 partners x 2 variants = 10
        # drafts, all seeded as needs_review.
        assert "draft_id=" in res.stdout


def test_approve_draft_moves_to_approved_to_send():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _stage7(ws)
        db = ws_dst / "data" / "pipeline.db"
        draft_id, pid = _draft_id_for_a_partner(db)

        # Approval gate (Finding 2) requires a partner email + valid
        # verification status. Set both so this test exercises the
        # happy-path approval rather than the gate refusal (which has
        # its own coverage in test_approval_gate.py).
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='operator@example.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--notes", "test approval",
             "--allow-example-domains"],
            capture_output=True, text=True,
            env={**os.environ, "USER": "test_approver"}, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "approved_to_send" in res.stdout

        # Pointer + event both moved.
        c = sqlite3.connect(db)
        status = c.execute(
            "select approval_status from email_drafts where draft_id = ?",
            (draft_id,),
        ).fetchone()[0]
        events = c.execute(
            "select event_type, actor, notes from draft_approvals "
            "where draft_id = ? order by event_id",
            (draft_id,),
        ).fetchall()
        c.close()
        assert status == "approved_to_send"
        assert [e[0] for e in events] == ["needs_review", "approved_to_send"]
        assert events[1][1] == "test_approver"
        assert events[1][2] == "test approval"


def test_approve_unknown_draft_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", "99999"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 1
        assert "not found" in res.stdout.lower()


def test_reject_draft_records_reason():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _stage7(ws)
        db = ws_dst / "data" / "pipeline.db"
        draft_id, _ = _draft_id_for_a_partner(db)

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "reject_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--reason", "off-base hook"],
            capture_output=True, text=True,
            env={**os.environ, "USER": "test_rejecter"}, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr

        c = sqlite3.connect(db)
        status, notes, actor = c.execute(
            "select e.approval_status, a.notes, a.actor "
            "from email_drafts e "
            "join draft_approvals a on a.draft_id = e.draft_id "
            "where e.draft_id = ? "
            "order by a.event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert status == "rejected"
        assert notes == "off-base hook"
        assert actor == "test_rejecter"


def test_approve_after_reject_is_blocked_by_state_machine():
    """The state machine requires rejected -> needs_review (un-reject)
    before approval. Direct rejected -> approved is invalid."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _stage7(ws)
        db = ws_dst / "data" / "pipeline.db"
        draft_id, _ = _draft_id_for_a_partner(db)

        # Reject first.
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "reject_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--reason", "first pass"],
            capture_output=True, text=True, timeout=60, check=True,
        )
        # Approve attempt should refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id)],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2
        assert "REFUSED" in res.stdout
        # State still rejected.
        c = sqlite3.connect(db)
        status = c.execute(
            "select approval_status from email_drafts where draft_id = ?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        assert status == "rejected"
