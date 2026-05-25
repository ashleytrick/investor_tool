"""Unit + integration tests for core/backups.py (Slice 5)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.conftest import REPO_ROOT, _run


from core.backups import (
    BACKUP_KEEP_PER_STAGE,
    backup_before_stage,
    list_backups,
    stages_needing_backup,
)


# ----- unit -----


def _write_db(path: Path, content: bytes = b"sqlite db content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_backup_writes_a_file_for_destructive_stage(tmp_path: Path) -> None:
    db = tmp_path / "data" / "pipeline.db"
    _write_db(db)
    out = backup_before_stage(
        tmp_path, stage="06_score_candidates", db_path=db,
    )
    assert out is not None
    assert out.exists()
    assert out.parent == tmp_path / "backups"
    # Stage tag embedded in filename.
    assert "06_score_candidates" in out.name


def test_backup_skipped_for_non_destructive_stage(tmp_path: Path) -> None:
    db = tmp_path / "data" / "pipeline.db"
    _write_db(db)
    out = backup_before_stage(
        tmp_path, stage="status", db_path=db,
    )
    assert out is None  # status is read-only; no backup
    assert not (tmp_path / "backups").exists()


def test_backup_skipped_when_db_does_not_exist(tmp_path: Path) -> None:
    """First-run case: nothing to back up before the DB is created."""
    out = backup_before_stage(
        tmp_path, stage="06_score_candidates",
        db_path=tmp_path / "data" / "pipeline.db",
    )
    assert out is None


def test_rotation_keeps_last_n(tmp_path: Path) -> None:
    db = tmp_path / "data" / "pipeline.db"
    _write_db(db)
    # Create BACKUP_KEEP_PER_STAGE + 3 backups; rotation should
    # keep exactly BACKUP_KEEP_PER_STAGE.
    for i in range(BACKUP_KEEP_PER_STAGE + 3):
        # Write a unique body so each backup hash differs and the
        # OS records distinct mtimes.
        _write_db(db, content=f"db-{i}".encode())
        backup_before_stage(
            tmp_path, stage="06_score_candidates", db_path=db,
        )
        # mtimes need to monotonically increase for the rotation to
        # pick the oldest correctly; nudge by sleeping briefly.
        time.sleep(0.01)
    surviving = list_backups(tmp_path, stage="06_score_candidates")
    assert len(surviving) == BACKUP_KEEP_PER_STAGE


def test_list_backups_filters_by_stage(tmp_path: Path) -> None:
    db = tmp_path / "data" / "pipeline.db"
    _write_db(db)
    backup_before_stage(
        tmp_path, stage="06_score_candidates", db_path=db,
    )
    time.sleep(0.01)
    backup_before_stage(
        tmp_path, stage="07_generate_emails", db_path=db,
    )
    all_b = list_backups(tmp_path)
    stage6_b = list_backups(tmp_path, stage="06_score_candidates")
    stage7_b = list_backups(tmp_path, stage="07_generate_emails")
    assert len(all_b) == 2
    assert len(stage6_b) == 1
    assert len(stage7_b) == 1


def test_stages_needing_backup_includes_known_destructive() -> None:
    """Guard against a future edit silently dropping a stage from
    the whitelist."""
    s = stages_needing_backup()
    for required in (
        "06_score_candidates", "07_generate_emails", "08_sync_to_attio",
        "import_partner_emails_apollo",
    ):
        assert required in s, (
            f"{required} must be in the backup whitelist"
        )


# ----- integration with stage_runner -----


def test_stage6_run_creates_backup_first(tmp_path: Path) -> None:
    """A real Stage 6 run on the fixture must produce a backup of
    the pipeline.db that existed BEFORE Stage 6 wrote."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    # Run pipeline up through Stage 5 so Stage 6 has inputs.
    for s, extra in (
        ("01_aggregate_sources.py", ()),
        ("02_enrich_funds.py", ("--fixtures",)),
        ("03_mine_activity.py", ("--fixtures",)),
        ("04_mine_partner_signals.py", ("--fixtures",)),
        ("05_verify_and_quality.py", ()),
    ):
        _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)
    # Stage 6 is destructive; should create a backup.
    _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)
    backups = list_backups(ws_dst, stage="06_score_candidates")
    assert len(backups) >= 1, "Stage 6 run should have backed up the db"


def test_restore_cli_lists_backups(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
    _run("02_enrich_funds.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "restore_db_backup.py"),
         "--workspace", ws],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "pipeline.db" in res.stdout


def test_restore_cli_swaps_in_the_chosen_backup(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
    _run("02_enrich_funds.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
    # Snapshot Stage 2's backup as the restore target. (Stage 2 backs
    # up the db state BEFORE Stage 2 wrote, i.e. just-after-Stage-1.)
    backups = list_backups(ws_dst, stage="02_enrich_funds")
    assert backups, "Stage 2 should have produced a backup"
    target = backups[0]

    # Mutate the live db so we can verify restore reverted it.
    db_path = ws_dst / "data" / "pipeline.db"
    c = sqlite3.connect(db_path)
    c.execute("insert into partners (partner_id, name) values ('marker', 'sentinel')")
    c.commit()
    c.close()

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "restore_db_backup.py"),
         "--workspace", ws, "--restore", target.name],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # The marker row should be gone after the restore.
    c = sqlite3.connect(db_path)
    rows = c.execute("select 1 from partners where partner_id = 'marker'").fetchall()
    c.close()
    assert rows == [], "restore should have removed the marker row"
    # A safety pre-restore copy was written.
    safety = [
        p for p in (ws_dst / "backups").iterdir()
        if p.name.startswith("pipeline.db.before_restore.")
    ]
    assert safety, "expected a safety pre-restore copy"
