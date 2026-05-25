"""Tests for the canonical approval gate (core/approval/gate.py + the
approve_draft CLI enforcement layer)."""
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


def _setup_workspace() -> tuple[Path, str, Path]:
    """Build a fresh fixture workspace through Stage 7 and return
    (ws_dst, ws_str, db_path). Caller cleans up tmpdir."""
    tmpdir = tempfile.mkdtemp()
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = Path(tmpdir) / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    _run_pipeline_through_stage_6(ws_dst)
    _stage7(ws)
    return ws_dst, ws, ws_dst / "data" / "pipeline.db"


def _draft_id(db: Path) -> tuple[int, str]:
    c = sqlite3.connect(db)
    row = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where is_recommended=1 limit 1"
    ).fetchone()
    c.close()
    return int(row[0]), row[1]


def test_approval_refused_when_partner_has_no_email():
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, _pid = _draft_id(db)
        # Fixture partner has no email -> gate should refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "REFUSED" in res.stdout
        assert "partner email is unknown" in res.stdout
        # Pointer must NOT have moved.
        c = sqlite3.connect(db)
        status = c.execute(
            "select approval_status from email_drafts where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        assert status == "needs_review"
    finally:
        shutil.rmtree(ws_dst.parent)


def test_approval_refused_when_do_not_contact_set():
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        # Give the partner a valid email but flag DNC.
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='ok@operator.com', "
            "email_verification_status='valid', do_not_contact=1, "
            "do_not_contact_reason='operator flagged' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        # Suppression appears as a blocker.
        assert (
            "relationship suppression" in res.stdout
            or "do_not_contact" in res.stdout
        )
    finally:
        shutil.rmtree(ws_dst.parent)


def test_approval_refused_when_email_verification_invalid():
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='bad@operator.com', "
            "email_verification_status='invalid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "verification status = invalid" in res.stdout
    finally:
        shutil.rmtree(ws_dst.parent)


def test_approval_refused_when_email_is_generic_mailbox():
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='info@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "generic" in res.stdout.lower() or "role" in res.stdout.lower()
    finally:
        shutil.rmtree(ws_dst.parent)


def test_override_blockers_requires_notes():
    """--override-blockers without --notes is refused even when the
    blockers are all soft. The notes field is the audit record of
    WHY the override was acceptable."""
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        # Clear hard blockers so the test reaches the --notes check
        # rather than refusing earlier on the hard-blocker rule.
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--override-blockers"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "requires --notes" in res.stdout
    finally:
        shutil.rmtree(ws_dst.parent)


def test_override_blockers_with_notes_succeeds_and_records_override():
    """Override on SOFT blockers (.example domains, generic mailbox)
    succeeds when --notes is provided. The overridden blockers are
    persisted structurally on the approval event so downstream gate
    re-checks honor the override."""
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        # Clear hard blockers: partner needs a valid email + verification.
        # Leave the fixture's .example founder / scheduling URLs in
        # place so the gate still raises SOFT blockers.
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--override-blockers",
             "--notes", "founder confirmed direct outreach OK"],
            capture_output=True, text=True,
            env={**os.environ, "USER": "operator"},
            timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        # Approval event row records the override prefix + the operator's
        # rationale + the structural overridden_blockers JSON list so
        # downstream consumers can honor the override.
        c = sqlite3.connect(db)
        notes, overridden = c.execute(
            "select notes, overridden_blockers from draft_approvals "
            "where draft_id=? and event_type='approved_to_send' "
            "order by event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert "OVERRODE BLOCKERS" in (notes or "")
        assert "founder confirmed" in notes
        # JSON list of soft blockers landed on the event.
        import json
        parsed = json.loads(overridden)
        assert isinstance(parsed, list) and len(parsed) >= 1
        # At least one .example-domain soft blocker should be in the list.
        assert any("example" in s.lower() for s in parsed)
    finally:
        shutil.rmtree(ws_dst.parent)


def test_override_refuses_hard_blockers():
    """--override-blockers must NOT bypass HARD blockers (missing
    email, do-not-contact, missing partner, etc.) even when --notes
    is provided. The operator has to fix the underlying state."""
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, _pid = _draft_id(db)
        # Fixture partner has no email -> hard blocker. Attempt an
        # override with notes -- still refused.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--override-blockers",
             "--notes", "operator wants to send anyway",
             "--allow-example-domains"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "HARD" in res.stdout
        assert "partner email is unknown" in res.stdout
        # Pointer must not have moved.
        c = sqlite3.connect(db)
        status = c.execute(
            "select approval_status from email_drafts where draft_id=?",
            (draft_id,),
        ).fetchone()[0]
        c.close()
        assert status == "needs_review"
    finally:
        shutil.rmtree(ws_dst.parent)


def test_downstream_gate_honors_override():
    """After --override-blockers approval, the downstream gate
    (export_send_queue / Gmail / Attio) using respect_overrides=True
    must NOT re-flag the same blockers. Tests that override metadata
    is durable across the gate round-trip."""
    ws_dst, ws, db = _setup_workspace()
    try:
        draft_id, pid = _draft_id(db)
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='op@operator.com', "
            "email_verification_status='valid' where partner_id=?",
            (pid,),
        )
        c.commit()
        c.close()
        # Approve with override (soft .example blockers acknowledged).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--override-blockers",
             "--notes", "fixture smoke test"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        # export_send_queue (respect_overrides=True) must NOT refuse.
        res2 = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "export_send_queue.py"),
             "--workspace", ws],
            capture_output=True, text=True, timeout=60,
        )
        assert res2.returncode == 0, (
            "send-queue export must honor the override; got:\n"
            + res2.stdout + res2.stderr
        )
        assert "1 approved" in res2.stdout
    finally:
        shutil.rmtree(ws_dst.parent)
