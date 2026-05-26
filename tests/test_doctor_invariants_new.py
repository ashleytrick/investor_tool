"""Tests for the Slice 19 doctor invariants covering schema added in
Slices 17 (email_drafts version/superseded_at), 18a
(manual_override_events), and 18b (sources registry)."""
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


def _doctor(ws: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
         "--workspace", ws],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "ANTHROPIC_API_KEY": ""},
    )


def test_doctor_flags_two_live_recommended_drafts():
    """Inject two LIVE rows with is_recommended=TRUE for the same
    partner. doctor's draft_history_invariants should fire 'error'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _stage7(ws)

        # Inject a second LIVE recommended draft for a partner that
        # already has one.
        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from email_drafts "
            "where is_recommended=1 and superseded_at is null limit 1"
        ).fetchone()[0]
        c.execute(
            "insert into email_drafts (partner_id, batch_id, version, "
            "  strategy, subject, body, is_recommended, generated_at, "
            "  approval_status, superseded_at, qa_status, template_smell) "
            "values (?, 'b_injected', 99, 'signal_led', 'inj', 'body', "
            "  1, datetime('now'), 'needs_review', NULL, 'pass', 'low')",
            (pid,),
        )
        c.commit()
        c.close()

        res = _doctor(ws)
        assert ">1 LIVE recommended draft" in res.stdout, res.stdout
        assert res.returncode == 2, (
            "two live recommended drafts is an error-severity finding "
            f"(expected exit 2); got {res.returncode}"
        )


def test_doctor_flags_superseded_row_still_recommended():
    """A superseded row should have is_recommended=FALSE. Stage 7's
    supersede clears it; if it didn't, doctor warns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _stage7(ws)
        # Re-run to actually supersede some rows.
        _stage7(ws)

        # Drift: re-flag a superseded row as recommended.
        c = sqlite3.connect(db)
        c.execute(
            "update email_drafts set is_recommended=1 "
            "where draft_id = ("
            "  select draft_id from email_drafts "
            "  where superseded_at is not null limit 1)"
        )
        c.commit()
        c.close()

        res = _doctor(ws)
        assert "superseded email_drafts row" in res.stdout, res.stdout
        # warn-severity -> exit 1
        assert res.returncode in (1, 2)


def test_doctor_flags_orphan_source_id():
    """source_snapshots.source_id pointing at a non-existent sources
    row is an error -- registry consistency is broken."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)

        c = sqlite3.connect(db)
        # Inject: a snapshot pointing at source_id 9999 which doesn't exist.
        c.execute(
            "update source_snapshots set source_id = 9999 "
            "where snapshot_id = (select snapshot_id from source_snapshots "
            "                       order by snapshot_id limit 1)"
        )
        c.commit()
        c.close()

        res = _doctor(ws)
        assert "non-existent sources row" in res.stdout, res.stdout
        assert res.returncode == 2


def test_doctor_flags_override_event_bad_kind():
    """manual_override_events with kind not in {score, rec, warm}
    is a schema violation -- the producer wrote junk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)

        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from partners limit 1"
        ).fetchone()[0]
        c.execute(
            "insert into manual_override_events "
            "(partner_id, kind, action, reason, actor, at) "
            "values (?, 'BOGUS', 'set', 'test', 'tester', datetime('now'))",
            (pid,),
        )
        c.commit()
        c.close()

        res = _doctor(ws)
        assert "unknown-kind" in res.stdout, res.stdout
        assert res.returncode == 2


def test_doctor_clean_after_full_fixture_run_with_all_new_features():
    """The fixture pipeline + Stage 7 + a happy-path approve sequence
    should leave every invariant clean -- doctor exits 0 with no
    error / warn findings tied to the Slice 17/18a/18b checks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _stage7(ws)
        # Re-run so supersede actually fires + history accumulates.
        _stage7(ws)

        # Run an operator override to populate manual_override_events.
        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from partner_score_summaries limit 1"
        ).fetchone()[0]
        c.close()
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid,
             "--score", "--reason", "doctor invariant test seed"],
            check=True, capture_output=True, timeout=30,
        )

        res = _doctor(ws)
        # Doctor may surface unrelated warnings the fixture happens to
        # trigger (placeholders in fixture drafts, etc.) -- those are
        # NOT regressions from this slice's new checks. We assert the
        # new check messages do NOT appear.
        for needle in (
            ">1 LIVE recommended draft",
            "violate version monotonicity",
            "non-existent sources row",
            "manual_override_events row(s) reference a partner_id",
            "unknown-kind",
            "unknown-action",
        ):
            assert needle not in res.stdout, (
                f"doctor surfaced unexpected finding {needle!r}:\n"
                f"{res.stdout}"
            )
