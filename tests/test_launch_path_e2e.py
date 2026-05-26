"""Dry-run end-to-end of the launch path: Apollo (manual) -> Stage 6 ->
Stage 7 -> approve_draft -> check_ready --for send -> export_send_queue.

This is the codified version of the "treat Gmail/export as the
primary launch path" verification. Sits on top of the per-stage unit
tests and asserts the full happy path stays glued together as
review-time fixes land.

Why a single combined test instead of decomposed steps: every fix in
this directory landed because some step worked in isolation but the
next step couldn't consume its output (or saw stale state). The whole
path needs to work end-to-end on a clean fixture so the operator can
trust the workflow.
"""
from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def test_launch_path_dry_run_e2e(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "e2e_ws"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    ws = str(ws_dst)
    env = {**os.environ, "USER": "tester"}

    # 1) Pipeline 01-06 (the helper) + Stage 7.
    _run_pipeline_through_stage_6(ws_dst)
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )

    # Pick a draft to approve.
    c = sqlite3.connect(db)
    draft_id, pid = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where is_recommended=1 and superseded_at is null limit 1"
    ).fetchone()
    c.close()

    # 2) Apollo / manual email: set the partner email so the approval
    # gate doesn't refuse on the canonical "email unknown" hard
    # blocker. (The stale-on-mutation logic from PR #28 will fire if
    # we later change this email -- exercised below.)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
         "--workspace", ws, "--partner-id", pid, "--email", "first@op.example"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # 3) Approve the draft. Use --override-blockers because the
    # fixture's batch-QA marks Stage 7's drafts qa_status='fail'
    # (per the fixture's stub LLM) -- but qa_fail is HARD so override
    # won't bypass it. Instead flip the draft's qa_status to 'pass'
    # directly so the approval represents a clean fixture send.
    c = sqlite3.connect(db)
    c.execute(
        "update email_drafts set qa_status='pass' where draft_id=?",
        (draft_id,),
    )
    c.execute(
        "update partners set email_verification_status='valid' "
        "where partner_id=?", (pid,),
    )
    c.commit()
    c.close()
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
         "--workspace", ws, "--draft-id", str(draft_id),
         "--notes", "e2e dry-run", "--allow-example-domains"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "approved_to_send" in res.stdout

    # 4) check_ready --for send must now pass (modulo mode=fixture).
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_ready.py"),
         "--workspace", ws, "--for", "send", "--allow-example-domains"],
        capture_output=True, text=True, timeout=60,
    )
    assert "have_approved_drafts: OK" in res.stdout, res.stdout
    assert "approved_gate_clean: OK" in res.stdout, res.stdout
    # mode=fixture is the only legitimate refusal on the fixture
    # workspace; everything else must be green.
    blocked_lines = [
        ln for ln in res.stdout.splitlines() if ": BLOCKED " in ln
    ]
    non_mode_blocked = [
        ln for ln in blocked_lines if not ln.startswith("[check_ready] mode:")
    ]
    assert not non_mode_blocked, (
        f"unexpected --for send blockers: {non_mode_blocked}"
    )

    # 5) Export send queue -- the canonical "what's about to go out"
    # CSV. Operator opens this before Gmail / external send.
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "export_send_queue.py"),
         "--workspace", ws, "--allow-example-domains"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    csv_path = ws_dst / "exports" / "send_queue.csv"
    assert csv_path.exists(), res.stdout
    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1, [r["draft_id"] for r in rows]
    row = rows[0]
    assert int(row["draft_id"]) == draft_id
    assert row["partner_email"] == "first@op.example"
    assert row["outreach_email_draft"], "draft body missing from CSV"
    assert row["draft_hash"], "draft_hash missing from CSV"

    # 6) Stale-on-mutation: change the partner email post-approval.
    # The approval must flip to stale_after_approval; the next
    # check_ready --for send must refuse on have_approved_drafts.
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
         "--workspace", ws, "--partner-id", pid,
         "--email", "second@op.example"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    c = sqlite3.connect(db)
    status = c.execute(
        "select approval_status from email_drafts where draft_id=?",
        (draft_id,),
    ).fetchone()[0]
    c.close()
    assert status == "stale_after_approval", (
        f"email change should have staled the approval; status={status}"
    )

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_ready.py"),
         "--workspace", ws, "--for", "send", "--allow-example-domains"],
        capture_output=True, text=True, timeout=60,
    )
    assert "have_approved_drafts: BLOCKED" in res.stdout, res.stdout

    # 7) Gmail draft path: with no Gmail credentials in the workspace,
    # create_gmail_drafts must SKIP cleanly (exit 0) rather than
    # blowing up. The check_ready --for gmail path tests the OAuth
    # not-linked branch as OK.
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "create_gmail_drafts.py"),
         "--workspace", ws, "--allow-example-domains",
         "--allow-fixture-mode"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Gmail not linked" in res.stdout, res.stdout
