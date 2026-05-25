"""Restore a SQLite backup taken by core/backups.py (Slice 5).

Lists available backups and, with --restore, replaces the live
pipeline.db with a chosen backup. Refuses to run while the
workspace run-lock is held (per Slice 4) so the operator can't
restore on top of a running stage and corrupt state.

Usage:
  # List backups, most recent first.
  uv run scripts/restore_db_backup.py --workspace clients/{name}

  # Restore a specific backup file.
  uv run scripts/restore_db_backup.py --workspace clients/{name} \\
      --restore pipeline.db.06_score_candidates.20260524T120000Z

The script also writes a safety-copy of the CURRENT db to
backups/pipeline.db.before_restore.{ts} before overwriting, so an
operator who restores the wrong file can step back.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.backups import (
    backup_path_for, list_backups,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.runlock import RunLockBusy, workspace_lock


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List or restore SQLite backups for this workspace.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--restore", default=None,
        help="Restore the named backup file (basename, not full path). "
             "Without --restore the script just lists what's available.",
    )
    parser.add_argument(
        "--stage", default=None,
        help="Filter the listing to backups tagged with this stage.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage="restore_db_backup")

    if args.restore is None:
        backups = list_backups(ws.path, stage=args.stage)
        if not backups:
            print("[restore] no backups found")
            return 0
        print(f"[restore] {len(backups)} backup(s) (most recent first):")
        for b in backups:
            print(f"  {b.name}  ({b.stat().st_size} bytes)")
        return 0

    target = ws.path / "backups" / args.restore
    if not target.exists():
        print(f"[restore] backup not found: {target}")
        return 1

    # Acquire the workspace lock so a concurrent stage can't write
    # over our restore mid-flight.
    try:
        with workspace_lock(ws.path, stage="restore_db_backup"):
            safety = (
                ws.path / "backups"
                / f"pipeline.db.before_restore."
                  f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            if ws.db_path.exists():
                shutil.copy2(ws.db_path, safety)
                print(f"[restore] safety copy: {safety.name}")
            shutil.copy2(target, ws.db_path)
            print(
                f"[restore] restored {target.name} -> "
                f"{ws.db_path}"
            )
    except RunLockBusy as exc:
        print(f"[restore] REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
