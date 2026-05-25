"""Unit + integration tests for core/runlock.py (Slice 4)."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.runlock import RunLockBusy, workspace_lock


# ----- unit -----


def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    with workspace_lock(tmp_path, stage="t1") as lock_path:
        # Lockfile exists while held.
        assert lock_path.exists()
        contents = lock_path.read_text(encoding="utf-8")
        assert "t1" in contents
        assert str(os.getpid()) in contents
    # After release the same workspace can be re-acquired.
    with workspace_lock(tmp_path, stage="t2"):
        pass


def test_second_acquire_in_same_process_raises(tmp_path: Path) -> None:
    """Two with-blocks for the same workspace in the same process
    means a stage is trying to run while another is holding -- the
    same race we want to prevent across processes."""
    with workspace_lock(tmp_path, stage="t1"):
        with pytest.raises(RunLockBusy):
            with workspace_lock(tmp_path, stage="t2"):
                pass


def test_lockfile_contents_include_pid_stage_and_timestamp(tmp_path: Path) -> None:
    with workspace_lock(tmp_path, stage="some_stage") as lock_path:
        body = lock_path.read_text(encoding="utf-8")
    parts = body.split("|")
    assert parts[0] == str(os.getpid())
    assert parts[1] == "some_stage"
    # ISO timestamp (best-effort parse).
    from datetime import datetime
    datetime.fromisoformat(parts[2])


def test_different_workspaces_dont_block_each_other(tmp_path: Path) -> None:
    """Two workspaces should be independently lockable."""
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    with workspace_lock(ws1, stage="t1"):
        # Different workspace -> no busy.
        with workspace_lock(ws2, stage="t2"):
            pass


def test_runlockbusy_message_names_the_holder(tmp_path: Path) -> None:
    with workspace_lock(tmp_path, stage="held_stage"):
        try:
            with workspace_lock(tmp_path, stage="second_stage"):
                pass
        except RunLockBusy as exc:
            msg = str(exc)
            assert "held_stage" in msg
            assert str(os.getpid()) in msg
        else:
            raise AssertionError("expected RunLockBusy")


# ----- integration with stage_run -----


def test_stage_run_refuses_when_lock_is_held(tmp_path: Path) -> None:
    """Run a real stage script while another process holds the lock
    on the same workspace. The blocked stage must exit non-zero with
    a REFUSED message; no `runs` row should land."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    # Run Stage 1 first so the DB exists.
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "01_aggregate_sources.py"),
         "--workspace", ws],
        capture_output=True, text=True,
        env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # Hold the lock from this test process.
    with workspace_lock(ws_dst, stage="test_holder"):
        # Try to launch Stage 1 again -- should refuse.
        res2 = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "01_aggregate_sources.py"),
             "--workspace", ws],
            capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
        )
        assert res2.returncode != 0, (
            f"stage_run should refuse when lock is held; got rc={res2.returncode}"
        )
        assert "REFUSED" in res2.stdout
        assert "test_holder" in res2.stdout


def test_lock_released_after_normal_stage_exit(tmp_path: Path) -> None:
    """After a stage completes cleanly, the lock is released and a
    follow-up stage in the same workspace can acquire it."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    # Two back-to-back Stage 1 runs (the second is a no-op rerun).
    for _ in range(2):
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "01_aggregate_sources.py"),
             "--workspace", ws],
            capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
