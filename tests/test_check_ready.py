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
    """Stage 7 ran, drafts exist (pending review only), and we patch
    mode to None. The review-phase checks should all pass -- send/gmail
    checks would reasonably block because nothing is approved yet."""
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

        # Default --for would be 'send' which requires approved drafts.
        # Use --for review since this workspace has only pending drafts.
        res = _check(ws, "--for", "review")
        assert "mode: OK" in res.stdout, res.stdout
        assert "approval_pipeline: OK" in res.stdout, res.stdout
        assert res.returncode == 0, res.stdout


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


def test_scheduling_link_reachable_skips_example_tld():
    """Slice 15: the reachability check returns OK without HTTP for
    .example / .test / .invalid / .localhost links -- the production
    guard already refuses those at send time, so they're noise here."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        # The fixture already uses cal.example. The scheduling check
        # only fires in --for gmail mode.
        res = _check(ws, "--for", "gmail")
        assert "scheduling_link_reachable: OK" in res.stdout, res.stdout


def test_scheduling_link_reachable_flags_broken_link():
    """When the scheduling link points at a non-existent host, the
    check fires BLOCKED. Use an RFC 6761 unreachable-by-design
    127.x address so no DNS lookup will succeed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        # Swap the scheduling link for an unreachable URL on a port
        # nothing listens to, with a short timeout.
        cfg_path = ws_dst / "config" / "company.yaml"
        cfg = cfg_path.read_text(encoding="utf-8")
        cfg = cfg.replace(
            'preferred_scheduling_link: "https://cal.example/dana-tendril"',
            'preferred_scheduling_link: "http://127.0.0.1:1/no-such-endpoint"',
        )
        assert "127.0.0.1:1" in cfg, "scheduling-link substitution failed"
        cfg_path.write_text(cfg, encoding="utf-8")

        res = _check(ws, "--for", "gmail")
        # Fixture mode also blocks, but the scheduling check should
        # specifically appear in the BLOCKED list.
        assert "scheduling_link_reachable: BLOCKED" in res.stdout, res.stdout


def test_gmail_oauth_check_skips_when_not_linked():
    """When the workspace has no .gmail_credentials.json, the check
    returns OK (not configured -- nothing to verify); operators who
    don't push to Gmail aren't blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        res = _check(ws, "--for", "gmail")
        assert "gmail_oauth: OK" in res.stdout, res.stdout
        assert "not linked" in res.stdout


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


def test_for_send_blocks_when_zero_approved_but_pending_exist():
    """--for send is a real pre-send green light: 10 pending review
    drafts + 0 approved is NOT ready to send. Default --for=send
    must surface this state explicitly via have_approved_drafts."""
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
        # Stage 7 produced pending-review drafts; nothing is approved.
        res = _check(ws, "--for", "send")
        assert res.returncode == 1, res.stdout
        assert "have_approved_drafts: BLOCKED" in res.stdout, res.stdout
        # And the review-phase view of the same workspace is OK on the
        # approval_pipeline check (pending drafts count as "operator
        # has something to do").
        res2 = _check(ws, "--for", "review")
        assert "approval_pipeline: OK" in res2.stdout, res2.stdout
        # have_approved_drafts shouldn't even run in review mode.
        assert "have_approved_drafts" not in res2.stdout, res2.stdout


def test_for_attio_requires_attio_config(monkeypatch=None):
    """--for attio adds an Attio-config reachability check. A workspace
    without ATTIO_API_KEY blocks specifically on attio_config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        # Run with ATTIO_API_KEY scrubbed from the env. The check must
        # fire even when other things also fail.
        env = {k: v for k, v in os.environ.items() if k != "ATTIO_API_KEY"}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_ready.py"),
             "--workspace", ws, "--for", "attio"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 1, res.stdout
        # The fixture has no attio.yaml AND we scrubbed ATTIO_API_KEY.
        # Either condition is enough for the attio_config check to
        # surface BLOCKED -- the point of the test is that --for attio
        # actually runs the check (other modes do not).
        assert "attio_config: BLOCKED" in res.stdout, res.stdout


def test_for_gmail_includes_scheduling_and_oauth_checks():
    """--for gmail adds scheduling + Gmail OAuth on top of send-mode
    checks. Earlier modes (review, send) skip them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        res_review = _check(ws, "--for", "review")
        assert "scheduling_link_reachable" not in res_review.stdout
        assert "gmail_oauth" not in res_review.stdout
        res_gmail = _check(ws, "--for", "gmail")
        assert "scheduling_link_reachable" in res_gmail.stdout
        assert "gmail_oauth" in res_gmail.stdout


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
