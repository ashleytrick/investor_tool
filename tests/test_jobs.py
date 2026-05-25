"""Stage-specific tests split out from tests/test_smoke.py.

Refactor item 23: per-stage test files so changes to one stage do not
churn a 4200-line monolith.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# REPO_ROOT, _run, _counts come from tests/conftest.py (Refactor item 24).
from tests.conftest import REPO_ROOT, _run, _counts





def test_jobs_produce_suggestions_and_apply():
    """monthly_learning_report seeds outcomes -> writes suggestions ->
    apply_axis_suggestion mutates axes.yaml + backs up + marks approved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        # Build the universe so axes/scores/outcomes have a substrate.
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
            ("06_score_candidates.py", ()),
            ("07_generate_emails.py", ("--top", "5")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # attio_outcome_sync skips cleanly without attio.yaml
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "attio_outcome_sync.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "skipping" in res.stdout

        # monthly_learning_report with seed -> at least 1 suggestion.
        # --include-fixture-outcomes is required because the seeded rows
        # are tagged source='fixture' and the learning report excludes
        # those by default (so a real workspace scaffolded from fixtures
        # doesn't silently train on toy data).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes",
             "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "suggestion" in res.stdout

        c = sqlite3.connect(db)
        n = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n >= 1, f"expected >=1 unapproved suggestion, got {n}"
        sid, ax_id, current_w, suggested_w = c.execute(
            "select suggestion_id, axis_id, current_weight, suggested_weight "
            "from axis_weight_suggestions order by suggestion_id limit 1"
        ).fetchone()
        c.close()

        # Confirm axes.yaml NOT yet mutated by the learning report.
        axes_yaml = ws_dst / "config" / "axes.yaml"
        original_text = axes_yaml.read_text()
        assert f"weight: {current_w}" in original_text or "weight: 1.0" in original_text

        # apply_axis_suggestion mutates + backs up + marks approved.
        # Fixture-generated suggestions are confidence=low (n=2); apply
        # path now requires --accept-low-confidence (Finding 67).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid),
             "--accept-low-confidence"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "backup=" in res.stdout

        # Re-running on the same now-approved suggestion is a no-op.
        res_again = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res_again.returncode == 0
        assert "already approved" in res_again.stdout

        # Backup file present
        backups = list((ws_dst / "config").glob("axes.yaml.bak.*"))
        assert len(backups) == 1, f"expected 1 backup, got {len(backups)}"

        # axes.yaml weight for the targeted axis is updated
        new_text = axes_yaml.read_text()
        assert new_text != original_text
        import yaml as _yaml
        loaded = _yaml.safe_load(new_text)
        target_axis = next(a for a in loaded["axes"] if a["id"] == ax_id)
        assert float(target_axis["weight"]) == float(suggested_w)

        # Re-applying same suggestion is a no-op
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "already approved" in res.stdout





def test_monthly_learning_excludes_fixture_outcomes_by_default():
    """Batch 8: outcomes with source='fixture' must NOT drive the
    learning report unless --include-fixture-outcomes is passed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
            ("06_score_candidates.py", ()),
            ("07_generate_emails.py", ("--top", "5")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Seed fixture outcomes, then run learning WITHOUT
        # --include-fixture-outcomes. Should exclude all the seeded rows
        # and report no usable outcomes (so no axis suggestions).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "excluded" in res.stdout and "source='fixture'" in res.stdout, (
            f"expected fixture-exclusion notice; stdout=\n{res.stdout}"
        )

        c = sqlite3.connect(db)
        n_sugs = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n_sugs == 0, (
            f"expected no suggestions when fixture outcomes excluded, got {n_sugs}"
        )

        # With --include-fixture-outcomes, suggestions ARE produced.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        n_sugs = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n_sugs >= 1, (
            f"expected >=1 suggestion with --include-fixture-outcomes, got {n_sugs}"
        )

        # All seeded rows must be tagged source='fixture'.
        n_fixture = c.execute(
            "select count(*) from outcomes where source='fixture'"
        ).fetchone()[0]
        assert n_fixture >= 1, "seeded outcomes must carry source='fixture'"
        c.close()





def test_batch15_apply_records_approver():
    """Batch 15: apply_axis_suggestion records approved_by + approval_reason
    on the row, and --list doesn't pollute run.processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
            ("06_score_candidates.py", ()),
            ("07_generate_emails.py", ("--top", "5",
                                        "--allow-example-domains")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Seed fixture outcomes and generate suggestions.
        subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes",
             "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )

        c = sqlite3.connect(db)
        sid = c.execute(
            "select suggestion_id from axis_weight_suggestions "
            "where approved is null order by suggestion_id limit 1"
        ).fetchone()
        assert sid is not None, "expected at least one pending suggestion"
        sid = sid[0]
        c.close()

        # --list: should NOT show processed in run.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--list"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        proc = c.execute(
            "select records_processed from runs where stage='apply_axis_suggestion' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        assert proc in (0, None), (
            f"--list should not record records_processed; got {proc}"
        )
        c.close()

        # Apply with --approved-by + --approval-reason
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid),
             "--accept-low-confidence",
             "--approved-by", "ashley", "--approval-reason",
             "validated against last 3 batches"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        approved_by, approval_reason = c.execute(
            "select approved_by, approval_reason from axis_weight_suggestions "
            "where suggestion_id=?", (sid,),
        ).fetchone()
        c.close()
        assert approved_by == "ashley"
        assert approval_reason == "validated against last 3 batches"
