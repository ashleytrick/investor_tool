"""Tests for Slice 18a manual_override audit event log."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def _pid_with_summary(db: Path) -> str:
    """Fixture partner with a partner_score_summaries row."""
    c = sqlite3.connect(db)
    pid = c.execute(
        "select partner_id from partner_score_summaries limit 1"
    ).fetchone()[0]
    c.close()
    return pid


def test_score_override_records_event(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    ws = str(ws_dst)
    pid = _pid_with_summary(db)

    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--score", "--reason", "ops bumped: insider tip"],
        check=True, capture_output=True,
        env={**os.environ, "USER": "alice"}, timeout=30,
    )

    c = sqlite3.connect(db)
    rows = c.execute(
        "select kind, action, reason, new_value, actor "
        "from manual_override_events where partner_id=? order by event_id",
        (pid,),
    ).fetchall()
    c.close()
    assert len(rows) == 1
    kind, action, reason, new_value, actor = rows[0]
    assert kind == "score"
    assert action == "set"
    assert "insider tip" in reason
    assert new_value is None  # score events don't carry a value
    assert actor == "alice"


def test_recommend_override_records_event_with_new_value(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    ws = str(ws_dst)
    pid = _pid_with_summary(db)

    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--recommend", "yes", "--reason", "founder confirmed"],
        check=True, capture_output=True,
        env={**os.environ, "USER": "bob"}, timeout=30,
    )

    c = sqlite3.connect(db)
    rows = c.execute(
        "select kind, action, reason, new_value, actor "
        "from manual_override_events where partner_id=? order by event_id",
        (pid,),
    ).fetchall()
    c.close()
    assert len(rows) == 1
    kind, action, reason, new_value, actor = rows[0]
    assert kind == "rec"
    assert action == "set"
    assert new_value == "yes"
    assert actor == "bob"


def test_clear_all_records_three_events(tmp_path: Path) -> None:
    """Default --clear (no per-flag scope) wipes score + rec + warm,
    so the event log should record three clear rows."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    ws = str(ws_dst)
    pid = _pid_with_summary(db)

    # Set score first so the clear has something to clear.
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--score", "--reason", "test seed"],
        check=True, capture_output=True, timeout=30,
    )

    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid, "--clear",
         "--reason", "rolling back"],
        check=True, capture_output=True, timeout=30,
    )

    c = sqlite3.connect(db)
    kinds = [
        r[0] for r in c.execute(
            "select kind from manual_override_events "
            "where partner_id=? and action='clear' order by event_id",
            (pid,),
        )
    ]
    c.close()
    assert kinds == ["score", "rec", "warm"]


def test_clear_with_scope_records_only_that_kind(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    ws = str(ws_dst)
    pid = _pid_with_summary(db)

    # Set both score and rec so clear has scope to be selective.
    for kind_arg in ("--score",):
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, kind_arg,
             "--reason", "seed"],
            check=True, capture_output=True, timeout=30,
        )
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--recommend", "yes", "--reason", "seed"],
        check=True, capture_output=True, timeout=30,
    )

    # Now clear only score.
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--clear", "--clear-score", "--reason", "score retry"],
        check=True, capture_output=True, timeout=30,
    )

    c = sqlite3.connect(db)
    cleared = [
        r[0] for r in c.execute(
            "select kind from manual_override_events "
            "where partner_id=? and action='clear' order by event_id",
            (pid,),
        )
    ]
    c.close()
    assert cleared == ["score"]


def test_list_override_events_cli_json(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    ws = str(ws_dst)
    pid = _pid_with_summary(db)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
         "--workspace", ws, "--partner-id", pid,
         "--score", "--reason", "for the list test"],
        check=True, capture_output=True,
        env={**os.environ, "USER": "carol"}, timeout=30,
    )
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "list_override_events.py"),
         "--workspace", ws, "--partner-id", pid, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    events = json.loads(res.stdout)
    assert len(events) == 1
    assert events[0]["kind"] == "score"
    assert events[0]["action"] == "set"
    assert events[0]["actor"] == "carol"
    assert events[0]["at"] is not None
