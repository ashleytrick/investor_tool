"""End-to-end tests for scripts/check_ready.py (Slice 3)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def _check(ws: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_ready.py"),
         "--workspace", ws, *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_fresh_fixture_workspace_is_blocked_because_fixture_mode():
    """A clean fixture workspace blocks on mode=fixture. The check
    surfaces the operator-actionable next step."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        res = _check(ws)
        # Fixture mode -> blocked.
        assert res.returncode == 1
        assert "mode: BLOCKED" in res.stdout
        assert "mode=fixture" in res.stdout


def test_blocks_when_stage_7_never_run():
    """No drafts at all means nothing to send AND nothing to review.
    The approval_pipeline check surfaces that."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        res = _check(ws)
        assert res.returncode == 1
        # Note: mode=fixture also blocks, but the approval_pipeline
        # check should also flag.
        assert "approval_pipeline: BLOCKED" in res.stdout
        assert "07_generate_emails" in res.stdout


def test_passes_when_pipeline_complete_and_mode_unset():
    """Stage 7 ran, drafts exist, and we patch mode to None (no
    explicit fixture/prod). All checks should pass."""
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
        # Remove mode key from company.yaml so the mode check
        # returns OK (mode=(unset)).
        cfg_path = ws_dst / "config" / "company.yaml"
        cfg = cfg_path.read_text(encoding="utf-8")
        cfg = cfg.replace("\nmode: fixture\n", "\n")
        cfg = cfg.replace("\nmode: \"fixture\"\n", "\n")
        cfg_path.write_text(cfg, encoding="utf-8")

        res = _check(ws)
        # The approval_pipeline check should pass; we have pending
        # drafts. mode=(unset) is OK.
        assert "mode: OK" in res.stdout, res.stdout
        assert "approval_pipeline: OK" in res.stdout, res.stdout


def test_blocks_when_approved_draft_has_no_email():
    """If an approved draft somehow has no partner email (shouldn't
    happen post-Slice-1 but defense in depth), check_ready flags it."""
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
        # Directly flip a draft to approved without setting partner
        # email -- simulates a stale approval that didn't get
        # invalidated.
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update email_drafts set approval_status='approved_to_send' "
            "where draft_id = ?", (draft_id,),
        )
        c.execute(
            "update partners set email=null where partner_id = ?",
            (pid,),
        )
        c.commit()
        c.close()

        res = _check(ws)
        assert res.returncode == 1
        assert "approved_have_emails: BLOCKED" in res.stdout
        assert f"draft_id={draft_id}" in res.stdout or "missing partner email" in res.stdout


def test_blocks_when_approved_draft_fails_live_gate():
    """Finding 3: the approved_gate_clean check re-runs the canonical
    approval gate against every approved_to_send draft. State that
    moved AFTER approval (DNC flipped on, verification regressed)
    must surface as BLOCKED so the operator notices before
    Gmail/Attio/CSV consumes the row."""
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
            "where is_recommended=1 limit 1"
        ).fetchone()
        # Approve with a valid email...
        c.execute(
            "update partners set email='op@op.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.execute(
            "update email_drafts set approval_status='approved_to_send' "
            "where draft_id=?", (draft_id,),
        )
        # ...then regress verification AFTER approval. The pointer
        # still says approved_to_send but the live gate now fails.
        c.execute(
            "update partners set email_verification_status='invalid' "
            "where partner_id=?", (pid,),
        )
        c.commit()
        c.close()
        res = _check(ws, "--allow-example-domains")
        assert res.returncode == 1
        assert "approved_gate_clean: BLOCKED" in res.stdout, res.stdout
        assert "invalid" in res.stdout


def test_blocks_when_two_approved_drafts_share_recipient():
    """Finding 3: a duplicate partner_email across approved drafts is
    surfaced so the operator catches data drift before sending the
    same person two emails."""
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
        # Two distinct recommended drafts on two distinct partners,
        # both approved, both pointing at the same email.
        rows = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 2"
        ).fetchall()
        assert len(rows) == 2
        for (did, pid) in rows:
            c.execute(
                "update partners set email='shared@op.com', "
                "email_verification_status='valid' where partner_id=?",
                (pid,),
            )
            c.execute(
                "update email_drafts set approval_status='approved_to_send' "
                "where draft_id=?", (did,),
            )
        c.commit()
        c.close()
        res = _check(ws, "--allow-example-domains")
        assert res.returncode == 1
        assert "no_duplicate_recipients: BLOCKED" in res.stdout, res.stdout


def test_blocks_when_dnc_partner_has_approved_draft():
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
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update email_drafts set approval_status='approved_to_send' "
            "where draft_id = ?", (draft_id,),
        )
        c.execute(
            "update partners set do_not_contact=1 where partner_id = ?",
            (pid,),
        )
        c.commit()
        c.close()

        res = _check(ws)
        assert res.returncode == 1
        assert "no_dnc_approvals: BLOCKED" in res.stdout


def test_quiet_mode_only_prints_blocked_and_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        res = _check(ws, "--quiet")
        # The summary line still prints; OK lines are suppressed.
        for line in res.stdout.splitlines():
            if line.startswith("[check_ready]") and ": OK -- " in line:
                raise AssertionError(
                    f"--quiet should suppress OK lines; got: {line}"
                )
        assert "passed," in res.stdout
