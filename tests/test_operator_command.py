"""Tests for the operator-command runner (Finding 4)."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side effect

from core.operator_command import operator_command_run
from core.runlock import workspace_lock


def _ws():
    tmp = Path(tempfile.mkdtemp())
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp / "test_workspace"
    shutil.copytree(src, dst)
    # Drop any pre-existing fixture DB so get_engine creates the
    # schema fresh on first operator_command_run invocation.
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    return dst


def test_operator_command_run_records_run_row():
    ws_dst = _ws()
    try:
        args = argparse.Namespace(workspace=str(ws_dst))
        with operator_command_run(args, stage="test_op_a") as ctx:
            ctx.run.note("did a thing")
            ctx.run.processed = 1
            ctx.run.succeeded = 1
        assert ctx.exit_code == 0
        # Run row landed.
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        row = c.execute(
            "select records_processed, records_succeeded, error_summary "
            "from runs where stage='test_op_a' "
            "order by run_id desc limit 1"
        ).fetchone()
        c.close()
        assert row == (1, 1, "did a thing")
    finally:
        shutil.rmtree(ws_dst.parent)


def test_operator_command_run_creates_pre_action_backup():
    ws_dst = _ws()
    try:
        # Init a real SQLite DB so backup_before_stage's shutil.copy2
        # produces a valid backup. (get_engine via operator_command_run
        # creates the schema on first connect.)
        args = argparse.Namespace(workspace=str(ws_dst))
        with operator_command_run(args, stage="approve_draft") as ctx:
            _ = ctx
        # First run created the DB but had no pre-existing file to
        # back up. Run again -- now there IS a pre-existing DB so a
        # backup should land.
        with operator_command_run(args, stage="approve_draft") as ctx:
            _ = ctx
        backups = list((ws_dst / "backups").glob("pipeline.db.approve_draft.*"))
        assert backups, "operator_command_run must produce a pre-action backup"
    finally:
        shutil.rmtree(ws_dst.parent)


def test_operator_command_run_holds_workspace_lock_against_concurrent_stage():
    """A second stage_run / operator_command_run against the same
    workspace must REFUSE while the first holds the lock."""
    ws_dst = _ws()
    try:
        held = workspace_lock(ws_dst, stage="07_generate_emails")
        held.__enter__()
        try:
            args = argparse.Namespace(workspace=str(ws_dst))
            # Operator command racing against an in-flight Stage 7
            # must exit immediately with REFUSED.
            with pytest.raises(SystemExit) as excinfo:
                with operator_command_run(args, stage="approve_draft") as ctx:
                    _ = ctx  # never reached
            assert excinfo.value.code == 2
        finally:
            held.__exit__(None, None, None)
    finally:
        shutil.rmtree(ws_dst.parent)


def test_operator_command_run_refuse_marks_failure_and_records_summary():
    ws_dst = _ws()
    try:
        args = argparse.Namespace(workspace=str(ws_dst))
        with operator_command_run(args, stage="test_op_refuse") as ctx:
            ctx.refuse("operator passed invalid input")
        # refuse() exits via run.failed > 0 -> OPERATIONAL_FAILURE = 2.
        assert ctx.exit_code == 2

        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        row = c.execute(
            "select records_failed, error_summary from runs "
            "where stage='test_op_refuse' order by run_id desc limit 1"
        ).fetchone()
        c.close()
        assert row[0] >= 1
        assert "operator passed invalid input" in (row[1] or "")
    finally:
        shutil.rmtree(ws_dst.parent)
