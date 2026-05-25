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





def test_refactor_batch_c_runlogger_accounting():
    """Refactor Batch C: run.attempt() / succeed() / skip() / fail()
    semantic helpers. is_clean() / all_skipped() introspection."""
    import argparse
    from core.stage_runner import stage_run

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        args = argparse.Namespace(workspace=str(ws_dst))

        # Mixed loop: 2 succeed (one implicit, one explicit), 1 skip,
        # 1 fail. Counters should match exactly.
        with stage_run(args, stage="test_accounting_mix",
                       require_llm=False) as ctx:
            run = ctx.run
            with run.attempt():
                pass  # implicit succeed
            with run.attempt():
                run.succeed()
            with run.attempt():
                run.skip("nothing to do for this record")
            with run.attempt():
                run.fail("rec_42", "TestErr", "synthetic")
        assert run.processed == 4
        assert run.succeeded == 2
        assert run.skipped == 1
        assert run.failed == 1
        assert ctx.exit_code == 2  # failed > 0

        # All-skipped detection.
        with stage_run(args, stage="test_accounting_all_skipped",
                       require_llm=False) as ctx:
            for _ in range(3):
                with ctx.run.attempt():
                    ctx.run.skip()
            assert ctx.run.all_skipped() is True
            assert ctx.run.is_clean() is True  # processed > 0, failed = 0

        # Clean run.
        with stage_run(args, stage="test_accounting_clean",
                       require_llm=False) as ctx:
            with ctx.run.attempt():
                pass
            assert ctx.run.is_clean() is True
        assert ctx.exit_code == 0

        # Uncaught exception inside attempt() must count as fail, not
        # succeed (regression test for the implicit-success bug where
        # the finally-only path treated unresolved attempts as wins).
        with stage_run(args, stage="test_accounting_uncaught",
                       require_llm=False) as ctx:
            for i in range(3):
                try:
                    with ctx.run.attempt():
                        if i == 1:
                            raise RuntimeError("boom")
                except RuntimeError:
                    pass
            assert ctx.run.processed == 3
            assert ctx.run.succeeded == 2
            assert ctx.run.failed == 1
        assert ctx.exit_code == 2





def test_refactor_batch_a_stage_runner_basic():
    """Refactor Batch A: stage_run() context manager. Smoke-test the
    happy path + the ctx.refuse() exit-code wiring."""
    import argparse
    from core.stage_runner import stage_run, StageContext

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        # Hand-build an args namespace to drive stage_run directly
        # (Stage 1's CLI uses it via argparse).
        args = argparse.Namespace(workspace=str(ws_dst))

        # Happy path: empty stage body, no failures, exit_code == 0.
        with stage_run(args, stage="test_runner_happy",
                       require_llm=False) as ctx:
            assert isinstance(ctx, StageContext)
            assert ctx.ws.path == ws_dst
            assert ctx.engine is not None
            assert ctx.llm is None  # require_llm=False
            assert ctx.run is not None
            ctx.run.processed = 3
            ctx.run.succeeded = 3
        assert ctx.exit_code == 0

        # Failure path: any run.failed -> exit_code 2.
        with stage_run(args, stage="test_runner_fail",
                       require_llm=False) as ctx:
            ctx.run.processed = 2
            ctx.run.succeeded = 1
            ctx.run.failed = 1
        assert ctx.exit_code == 2

        # Refuse path: ctx.refuse() sets exit_code AND records a note.
        # Refactor Batch B: kwarg is `code=StageResult.*` now.
        from core.stage_result import StageResult
        with stage_run(args, stage="test_runner_refuse",
                       require_llm=False) as ctx:
            ctx.refuse(
                "synthetic safety gate fired",
                code=StageResult.OPERATIONAL_FAILURE,
            )
        assert ctx.exit_code == int(StageResult.OPERATIONAL_FAILURE)

        # refuse_unsafe() shorthand maps to StageResult.REFUSED_UNSAFE (3).
        with stage_run(args, stage="test_runner_refuse_unsafe",
                       require_llm=False) as ctx:
            ctx.refuse_unsafe("safety gate -- explicit unsafe code")
        assert ctx.exit_code == int(StageResult.REFUSED_UNSAFE) == 3

        # Verify the runs table got four rows (happy, fail, refuse,
        # refuse_unsafe). Each writes records_failed appropriately.
        c = sqlite3.connect(db)
        rows = c.execute(
            "select stage, records_failed, error_summary "
            "from runs where stage like 'test_runner_%' order by run_id"
        ).fetchall()
        c.close()
        assert len(rows) == 4
        assert rows[0][1] == 0       # happy: failed=0
        assert rows[1][1] == 1       # fail: failed=1
        assert rows[2][1] == 1       # refuse: forced to 1
        assert "synthetic safety gate fired" in (rows[2][2] or "")
        assert rows[3][1] == 1       # refuse_unsafe: forced to 1
        assert "explicit unsafe" in (rows[3][2] or "")


def test_preflight_failure_lands_in_runs_not_silent_sys_exit():
    """Two-part contract for preflight refusal:

    1. The refusal lands in `runs` (was a silent sys.exit before the
       launch-blocker fix landed -- error_summary now carries the
       REFUSED note + missing-file detail).
    2. The stage body MUST NOT execute after a refusal. stage_run
       raises SystemExit so the caller's `with` block never runs;
       previously it yielded the refused ctx and the body ran against
       a half-initialized workspace.
    """
    import argparse
    import shutil
    import sqlite3
    import tempfile
    from pathlib import Path

    import pytest

    from core.stage_runner import stage_run

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        # Corrupt the workspace config so preflight refuses. Remove
        # axes.yaml -- that triggers the "config/axes.yaml missing"
        # issue in validate_workspace_config.
        (ws_dst / "config" / "axes.yaml").unlink()

        args = argparse.Namespace(workspace=str(ws_dst))
        body_ran = {"flag": False}
        with pytest.raises(SystemExit) as excinfo:
            with stage_run(
                args, stage="test_preflight_refusal", require_llm=False,
            ) as ctx:
                body_ran["flag"] = True
                _ = ctx  # silence unused warnings
        # Refusal exits with REFUSED_UNSAFE (=3).
        assert excinfo.value.code == 3
        # The stage body never ran -- the SystemExit was raised inside
        # stage_run before the contextmanager yielded.
        assert body_ran["flag"] is False, (
            "stage body executed after preflight refusal; the abort "
            "guard regressed"
        )

        # Run row landed with the refusal note.
        c = sqlite3.connect(db)
        row = c.execute(
            "select records_failed, error_summary from runs "
            "where stage='test_preflight_refusal' order by run_id desc limit 1"
        ).fetchone()
        c.close()
        assert row is not None, (
            "preflight refusal must produce a runs row -- the prior "
            "shape sys.exit'd before RunLogger opened"
        )
        records_failed, error_summary = row
        assert records_failed >= 1
        assert error_summary and "REFUSED" in error_summary
        assert "axes.yaml" in error_summary
        # SystemExit must NOT be logged as a fatal "__run__" error.
        # (The refuse_unsafe note carries the operator-facing message;
        # a phantom "SystemExit: 3" line would confuse audits.)
        assert "SystemExit" not in (error_summary or "")
