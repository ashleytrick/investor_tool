"""Tests for pipeline-spanning batch lineage (Issue #19)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import REPO_ROOT

from core.batch_ids import (
    batch_exists,
    create_pipeline_batch,
    finalize_pipeline_batch,
    mint_batch_id,
)
from core.db import get_engine, pipeline_batches


@pytest.fixture
def engine(tmp_path: Path):
    return get_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_mint_batch_id_returns_hex_token():
    bid = mint_batch_id()
    assert isinstance(bid, str)
    # token_hex(16) -> 32 hex chars.
    assert len(bid) == 32
    int(bid, 16)  # raises if not hex.


def test_create_pipeline_batch_inserts_row(engine) -> None:
    with engine.begin() as conn:
        bid = create_pipeline_batch(
            conn, workspace="oko", operator="ashley",
            notes="weekly Tuesday pass",
        )
    with engine.begin() as conn:
        row = conn.execute(
            select(pipeline_batches).where(pipeline_batches.c.batch_id == bid)
        ).first()
    assert row is not None
    assert row.workspace == "oko"
    assert row.operator == "ashley"
    assert row.notes == "weekly Tuesday pass"
    assert row.started_at is not None
    assert row.completed_at is None


def test_batch_exists(engine) -> None:
    with engine.begin() as conn:
        assert batch_exists(conn, "deadbeef") is False
        bid = create_pipeline_batch(conn, workspace="x")
        assert batch_exists(conn, bid) is True


def test_finalize_pipeline_batch_stamps_completed_at(engine) -> None:
    with engine.begin() as conn:
        bid = create_pipeline_batch(conn, workspace="x")
    with engine.begin() as conn:
        finalize_pipeline_batch(conn, batch_id=bid)
    with engine.begin() as conn:
        row = conn.execute(
            select(pipeline_batches.c.completed_at)
            .where(pipeline_batches.c.batch_id == bid)
        ).first()
    assert row.completed_at is not None


def test_new_pipeline_batch_cli_quiet_mode(tmp_path):
    """`--quiet` prints just the batch_id so an operator can shell-capture
    it: BATCH=$(uv run scripts/new_pipeline_batch.py --quiet ...)."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "new_pipeline_batch.py"),
         "--workspace", str(ws_dst), "--quiet"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    bid = res.stdout.strip()
    assert len(bid) == 32
    int(bid, 16)
    # Row in pipeline_batches.
    c = sqlite3.connect(db)
    row = c.execute(
        "select batch_id, workspace from pipeline_batches where batch_id=?",
        (bid,),
    ).fetchone()
    c.close()
    assert row is not None
    # Workspace name disambiguation appends a hash; just check the prefix.
    assert row[1].startswith("test_workspace")


def test_stage_run_stamps_pipeline_batch_id_on_runs(tmp_path):
    """Pass --pipeline-batch to a stage; the runs row carries the id."""
    import argparse
    from core.stage_runner import stage_run

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    # Mint a batch via the helper first.
    engine = get_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        bid = create_pipeline_batch(conn, workspace="test_workspace")

    args = argparse.Namespace(workspace=str(ws_dst), pipeline_batch=bid)
    with stage_run(args, stage="test_batch_stamp", require_llm=False) as ctx:
        ctx.run.note("did the thing")

    c = sqlite3.connect(db)
    row = c.execute(
        "select pipeline_batch_id, error_summary from runs "
        "where stage='test_batch_stamp' order by run_id desc limit 1"
    ).fetchone()
    c.close()
    assert row[0] == bid


def test_stage_run_refuses_unknown_pipeline_batch_id(tmp_path):
    """An operator typo in --pipeline-batch should refuse cleanly,
    not silently create an orphan run linked to a bogus batch."""
    import argparse
    from core.stage_runner import stage_run

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    args = argparse.Namespace(workspace=str(ws_dst),
                              pipeline_batch="000000nope000000")
    with pytest.raises(SystemExit) as exc:
        with stage_run(args, stage="test_batch_refuse", require_llm=False) as ctx:
            _ = ctx  # never reached
    # USAGE_ERROR = 1
    assert exc.value.code == 1


def test_list_pipeline_batches_cli_shows_linked_runs(tmp_path):
    """End-to-end smoke: mint a batch via the CLI, run a stage against
    it, then list_pipeline_batches.py --json shows the batch with the
    linked run. Use the CLI to mint so the workspace name matches what
    list_pipeline_batches sees (load_workspace assigns a per-dir
    disambiguating suffix)."""
    import argparse
    from core.stage_runner import stage_run

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "new_pipeline_batch.py"),
         "--workspace", str(ws_dst), "--quiet", "--notes", "cli smoke"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    bid = res.stdout.strip()

    # Drive a stage so the runs row gets stamped.
    args = argparse.Namespace(workspace=str(ws_dst), pipeline_batch=bid)
    with stage_run(args, stage="test_listing", require_llm=False) as ctx:
        ctx.run.processed = 1
        ctx.run.succeeded = 1

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "list_pipeline_batches.py"),
         "--workspace", str(ws_dst), "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    batches = json.loads(res.stdout)
    assert len(batches) == 1
    b = batches[0]
    assert b["batch_id"] == bid
    assert b["notes"] == "cli smoke"
    assert len(b["runs"]) == 1
    assert b["runs"][0]["stage"] == "test_listing"
    assert b["runs"][0]["records_processed"] == 1
