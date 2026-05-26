"""Regression tests for the post-PR-26 finding batch: manual mutations
that move the recipient or the right-to-send must invalidate any prior
approval.

Each test approves a draft, runs the manual-mutation CLI under test,
and asserts that:
  - the draft's approval_status flipped to stale_after_approval
  - a draft_approvals event with the right trigger was recorded
  - if applicable, the approval gate now refuses approval HARD (no
    --override-blockers escape)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def _setup_with_approved_draft(tmp_path: Path) -> tuple[Path, str, int, str]:
    """Build a fixture workspace with ONE approved_to_send draft.
    Returns (db_path, workspace_str, draft_id, partner_id)."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    ws = str(ws_dst)
    _run_pipeline_through_stage_6(ws_dst)
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )
    c = sqlite3.connect(db)
    draft_id, pid = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where is_recommended=1 and superseded_at is null limit 1"
    ).fetchone()
    # Set a clean email + valid verification so the approval is
    # otherwise gate-clean.
    c.execute(
        "update partners set email='op@op.com', "
        "email_verification_status='valid' where partner_id=?",
        (pid,),
    )
    c.execute(
        "update email_drafts set approval_status='approved_to_send' "
        "where draft_id=?", (draft_id,),
    )
    # Mirror the approval into draft_approvals so transition logic can
    # see the "from" state.
    c.execute(
        "insert into draft_approvals(draft_id, partner_id, event_type, "
        "actor, at, draft_hash) values (?, ?, 'approved_to_send', "
        "'tester', datetime('now'), 'h')",
        (draft_id, pid),
    )
    c.commit()
    c.close()
    return db, ws, draft_id, pid


def _approval_status(db: Path, draft_id: int) -> str:
    c = sqlite3.connect(db)
    row = c.execute(
        "select approval_status from email_drafts where draft_id=?",
        (draft_id,),
    ).fetchone()
    c.close()
    return row[0]


def _latest_event_type(db: Path, draft_id: int) -> str:
    c = sqlite3.connect(db)
    row = c.execute(
        "select event_type from draft_approvals where draft_id=? "
        "order by event_id desc limit 1",
        (draft_id,),
    ).fetchone()
    c.close()
    return row[0]


def test_set_partner_email_stales_approval_when_email_changes(tmp_path: Path) -> None:
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
         "--workspace", ws, "--partner-id", pid,
         "--email", "new-recipient@op.com"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _approval_status(db, draft_id) == "stale_after_approval", res.stdout
    assert _latest_event_type(db, draft_id) == "stale_after_approval"


def test_set_partner_email_no_change_no_stale(tmp_path: Path) -> None:
    """Writing the SAME email shouldn't fire a stale event -- the
    recipient didn't change."""
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
         "--workspace", ws, "--partner-id", pid, "--email", "op@op.com"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _approval_status(db, draft_id) == "approved_to_send"


def test_set_do_not_contact_stales_approval(tmp_path: Path) -> None:
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_do_not_contact.py"),
         "--workspace", ws, "--partner-id", pid,
         "--reason", "conflict of interest"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _approval_status(db, draft_id) == "stale_after_approval"


def test_set_employment_left_fund_stales_approval(tmp_path: Path) -> None:
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_employment_status.py"),
         "--workspace", ws, "--partner-id", pid,
         "--status", "left_fund", "--reason", "Twitter bio updated"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _approval_status(db, draft_id) == "stale_after_approval"


def test_employment_left_fund_is_hard_blocker_in_gate(tmp_path: Path) -> None:
    """After left_fund, the approval gate must hard-refuse so the
    operator can't re-approve without first re-verifying the role.
    """
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    # Flip status outside the CLI so the partner table reflects
    # left_fund without already staling the draft (we want to test
    # the gate's own refusal, not the CLI's invalidation).
    c = sqlite3.connect(db)
    c.execute(
        "update partners set employment_status='left_fund' "
        "where partner_id=?", (pid,),
    )
    c.execute(
        "update email_drafts set approval_status='needs_review' "
        "where draft_id=?", (draft_id,),
    )
    c.commit()
    c.close()
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.approval.gate import can_approve_draft, classify_blocker
    workspace = load_workspace(ws)
    engine = get_engine(f"sqlite:///{db}")
    gate = can_approve_draft(
        workspace, engine, draft_id, allow_example_domains=True,
    )
    assert gate.ok is False
    assert any("left_fund" in b for b in gate.blockers), gate.blockers
    # HARD -- cannot be bypassed.
    assert any(
        classify_blocker(b) == "hard"
        for b in gate.blockers if "left_fund" in b
    )


def test_set_fund_inactive_stales_all_partners_in_fund(tmp_path: Path) -> None:
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    c = sqlite3.connect(db)
    fund_id = c.execute(
        "select fund_id from partners where partner_id=?", (pid,)
    ).fetchone()[0]
    c.close()
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_fund_inactive.py"),
         "--workspace", ws, "--fund-id", fund_id,
         "--reason", "no new deals in 24 months"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _approval_status(db, draft_id) == "stale_after_approval"


def test_fund_inactive_is_hard_blocker_in_gate(tmp_path: Path) -> None:
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    c = sqlite3.connect(db)
    fund_id = c.execute(
        "select fund_id from partners where partner_id=?", (pid,)
    ).fetchone()[0]
    c.execute("update funds set is_active=0 where fund_id=?", (fund_id,))
    c.execute(
        "update email_drafts set approval_status='needs_review' "
        "where draft_id=?", (draft_id,),
    )
    c.commit()
    c.close()
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.approval.gate import can_approve_draft, classify_blocker
    workspace = load_workspace(ws)
    engine = get_engine(f"sqlite:///{db}")
    gate = can_approve_draft(
        workspace, engine, draft_id, allow_example_domains=True,
    )
    assert gate.ok is False
    assert any("fund is inactive" in b for b in gate.blockers), gate.blockers
    assert any(
        classify_blocker(b) == "hard"
        for b in gate.blockers if "fund is inactive" in b
    )


def test_apollo_import_refuses_duplicate_partner_id(tmp_path: Path) -> None:
    """The unique_field validator should fail a CSV that lists the
    same partner_id twice -- preventing last-write-wins overwrites
    in a single import."""
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    csv_path = tmp_path / "apollo.csv"
    csv_path.write_text(
        "partner_id,email\n"
        f"{pid},first@op.com\n"
        f"{pid},second@op.com\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "import_partner_emails_apollo.py"),
         "--workspace", ws, "--from-csv", str(csv_path), "--overwrite"],
        capture_output=True, text=True, timeout=60,
    )
    # CSV had a duplicate -- importer's exit code reflects row errors.
    assert res.returncode != 0, res.stdout + res.stderr
    assert "duplicate" in res.stdout.lower(), res.stdout


def test_record_outcome_hydrates_relationship_and_stales(tmp_path: Path) -> None:
    """A manual outcome of status=replied / reply_type=passed_too_early
    moves the relationship to suppressed; the still-approved draft
    must stale automatically via the persist_outcome_event suppression
    tail."""
    db, ws, draft_id, pid = _setup_with_approved_draft(tmp_path)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "record_outcome.py"),
         "--workspace", ws, "--partner-id", pid,
         "--status", "replied", "--reply-type", "passed_too_early"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    c = sqlite3.connect(db)
    rs, last_reply = c.execute(
        "select relationship_status, last_reply_at from partners "
        "where partner_id=?", (pid,),
    ).fetchone()
    c.close()
    # Hydration ran (last_reply_at populated; relationship_status moved
    # off the default).
    assert last_reply is not None, "hydration didn't run"
    # Approval should be stale because the passed outcome suppresses.
    assert _approval_status(db, draft_id) == "stale_after_approval", (
        f"expected stale; got status={_approval_status(db, draft_id)} "
        f"relationship={rs}"
    )
