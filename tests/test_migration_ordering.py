"""Regression tests for the migration / lock / backup ordering finding.

Two protections the operator depends on for safe schema upgrades:

  1. The workspace lock must be acquired BEFORE get_engine() runs
     migrations. Two stages started concurrently against the same
     workspace must NOT race on ALTER TABLE / migration backfills --
     the second one blocks on the lock until the first completes.

  2. A pre-migration backup must be taken BEFORE get_engine() can
     mutate the schema. On a real workspace, that gives the
     operator a "what did the DB look like before today's
     migrations touched it?" snapshot to restore from.

Both protections apply to BOTH stage_run (pipeline stages) and
operator_command_run (manual CLIs).
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT


def _fresh_ws(tmp_path: Path) -> Path:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    return ws_dst


def test_pre_migration_backup_runs_for_existing_db_via_stage_run(tmp_path: Path) -> None:
    """When the workspace already has a pipeline.db, stage_run must
    snapshot it to backups/pipeline.db.pre_migration.* BEFORE
    get_engine() runs."""
    from core.db import get_engine
    from core.stage_runner import stage_run

    ws_dst = _fresh_ws(tmp_path)
    # Make sure a DB file exists so the pre_migration backup has
    # something to copy. Touch via get_engine -- that creates a
    # baseline DB the next stage_run will treat as upgrade target.
    get_engine(f"sqlite:///{ws_dst / 'data' / 'pipeline.db'}")
    assert (ws_dst / "data" / "pipeline.db").exists()

    args = argparse.Namespace(workspace=str(ws_dst))
    with stage_run(args, stage="test_pre_migration", require_llm=False):
        pass

    backups = list((ws_dst / "backups").glob("pipeline.db.pre_migration.*"))
    assert backups, (
        "expected a pre_migration backup; found: "
        f"{[p.name for p in (ws_dst / 'backups').iterdir()]}"
    )


def test_pre_migration_backup_skips_fresh_workspace(tmp_path: Path) -> None:
    """No DB file = nothing to back up. stage_run should not error
    and should not create a phantom backup file."""
    from core.stage_runner import stage_run

    ws_dst = _fresh_ws(tmp_path)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    args = argparse.Namespace(workspace=str(ws_dst))
    with stage_run(args, stage="test_fresh", require_llm=False):
        pass
    backups_dir = ws_dst / "backups"
    pre_migration = (
        list(backups_dir.glob("pipeline.db.pre_migration.*"))
        if backups_dir.is_dir() else []
    )
    assert not pre_migration, (
        "fresh workspaces should not generate a pre_migration backup"
    )


def test_lock_acquired_before_get_engine_in_stage_run(tmp_path: Path) -> None:
    """While stage_run holds the lock, a second stage_run against
    the same workspace must REFUSE with the lock-busy message.
    The refusal must fire before get_engine() runs, so even if
    the second invocation would otherwise migrate the schema, it
    can't race the first.
    """
    from core.runlock import RunLockBusy, workspace_lock
    from core.stage_runner import stage_run

    ws_dst = _fresh_ws(tmp_path)
    args = argparse.Namespace(workspace=str(ws_dst))
    # Hold the lock outside stage_run; the inner stage_run call
    # must refuse to enter.
    held = workspace_lock(ws_dst, stage="held_by_other")
    held.__enter__()
    try:
        with pytest.raises(SystemExit) as exc_info:
            with stage_run(args, stage="should_refuse", require_llm=False):
                pytest.fail("stage_run should not have entered the body")
        assert int(exc_info.value.code) != 0, (
            "lock-busy refusal should produce a non-zero exit"
        )
    finally:
        held.__exit__(None, None, None)


def test_lock_acquired_before_get_engine_in_operator_command(tmp_path: Path) -> None:
    """Same protection for operator_command_run -- a held lock
    blocks the operator CLI before get_engine() can mutate."""
    from core.runlock import workspace_lock
    from core.operator_command import operator_command_run

    ws_dst = _fresh_ws(tmp_path)
    args = argparse.Namespace(workspace=str(ws_dst))
    held = workspace_lock(ws_dst, stage="held_by_other")
    held.__enter__()
    try:
        with pytest.raises(SystemExit) as exc_info:
            with operator_command_run(args, stage="op_refuse_test"):
                pytest.fail("operator_command_run should not have entered")
        assert int(exc_info.value.code) != 0
    finally:
        held.__exit__(None, None, None)


def test_pre_migration_backup_helper_returns_none_for_missing_db(tmp_path: Path) -> None:
    """Direct unit on the helper: missing source DB -> None, no
    backups directory created."""
    from core.backups import pre_migration_backup

    ws_dst = _fresh_ws(tmp_path)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    out = pre_migration_backup(ws_dst, db_path=db)
    assert out is None


def test_lock_released_when_get_engine_raises_in_stage_run(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pre-PR-29 review finding: if get_engine() or
    pre_migration_backup() raises after the lock is acquired, the
    lock must still be released. A held lock from a crashed test
    or a long-lived process leaks until process exit; verify the
    try/finally covers the setup phase.
    """
    import core.stage_runner as sr
    from core.runlock import workspace_lock
    from core.stage_runner import stage_run
    import argparse

    ws_dst = _fresh_ws(tmp_path)
    # Make get_engine blow up so we exercise the failure path.

    def _boom(*_a, **_kw):  # noqa: ANN001 -- test helper
        raise RuntimeError("simulated migration failure")
    monkeypatch.setattr(sr, "get_engine", _boom)

    args = argparse.Namespace(workspace=str(ws_dst))
    with pytest.raises(RuntimeError, match="simulated migration"):
        with stage_run(args, stage="boom_test", require_llm=False):
            pytest.fail("body should not have entered")

    # The lock should be free now -- a brand-new workspace_lock
    # acquisition must NOT block.
    try:
        held = workspace_lock(ws_dst, stage="probe")
        held.__enter__()
        held.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"lock leaked after get_engine failure: {exc}")


def test_lock_released_when_get_engine_raises_in_operator_command(
    tmp_path: Path, monkeypatch,
) -> None:
    """Same protection for operator_command_run."""
    import argparse
    import core.operator_command as oc
    from core.operator_command import operator_command_run
    from core.runlock import workspace_lock

    ws_dst = _fresh_ws(tmp_path)
    monkeypatch.setattr(
        oc, "get_engine",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError("simulated migration failure"),
        ),
    )
    args = argparse.Namespace(workspace=str(ws_dst))
    with pytest.raises(RuntimeError, match="simulated migration"):
        with operator_command_run(args, stage="boom_op"):
            pytest.fail("body should not have entered")
    try:
        held = workspace_lock(ws_dst, stage="probe")
        held.__enter__()
        held.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"lock leaked after get_engine failure: {exc}")


def test_pre_migration_backup_rotates_keeping_latest(tmp_path: Path) -> None:
    """The helper reuses the standard rotation -- only the latest
    BACKUP_KEEP_PER_STAGE pre_migration snapshots survive."""
    from core.backups import BACKUP_KEEP_PER_STAGE, pre_migration_backup
    from core.db import get_engine

    ws_dst = _fresh_ws(tmp_path)
    db = ws_dst / "data" / "pipeline.db"
    get_engine(f"sqlite:///{db}")
    # Take more snapshots than the rotation allows.
    for _ in range(BACKUP_KEEP_PER_STAGE + 3):
        out = pre_migration_backup(ws_dst, db_path=db)
        assert out is not None
    survivors = list(
        (ws_dst / "backups").glob("pipeline.db.pre_migration.*")
    )
    assert len(survivors) <= BACKUP_KEEP_PER_STAGE
