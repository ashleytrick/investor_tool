"""Pre-stage SQLite database backup (Slice 5).

Cheap insurance against destructive stage bugs while the schema is
still churning. Before Stage 6 / 7 / 8 / Apollo-import runs, copy
`pipeline.db` to `clients/{name}/backups/pipeline.db.{stage}.{ts}`
and keep the last N. If a stage corrupts data, the operator restores
from the prior backup via `scripts/restore_db_backup.py`.

Design notes:

  - Pure file copy via shutil.copy2 (preserves mtime). SQLite WAL
    files are flushed on the prior stage's normal exit; this is
    "good enough" for the current single-writer pattern. If we later
    move to WAL with concurrent readers, switch to sqlite3's
    backup() API.
  - Rotation: keep last BACKUP_KEEP_PER_STAGE per (workspace, stage).
    Oldest get unlinked.
  - Failure modes: a backup failure does NOT block the stage --
    print a warning, record a note in `runs`, continue. The
    operator sees the warning and can hand-copy if they care.

The `stages_needing_backup()` whitelist is the single place to
extend coverage. Read-only stages (status, doctor) are NOT in the
list so they don't churn backups.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

# How many backups to keep per (workspace, stage). Older ones rotate
# out. 5 is enough to walk back through a bad-batch debug session
# without flooding the disk; tweak per workspace if needed.
BACKUP_KEEP_PER_STAGE: int = 5

# Stages whose run rewrites scores / drafts / DB schema and is worth
# a pre-stage backup. Read-only stages (status, doctor, list_*) do
# NOT trigger a backup so they don't churn the backup directory.
_DESTRUCTIVE_STAGES: frozenset[str] = frozenset({
    "02_enrich_funds",
    "03_mine_activity",
    "04_mine_partner_signals",
    "05_verify_and_quality",
    "06_score_candidates",
    "07_generate_emails",
    "08_sync_to_attio",
    "import_partner_emails_apollo",
    "apply_axis_suggestion",
    "monthly_learning_report",
    "manual_override",
    "record_outcome",
    "set_do_not_contact",
    "set_partner_email",
    "attio_outcome_sync",
    # Finding 4: operator CLIs that mutate state via
    # core.operator_command.operator_command_run also need pre-action
    # backups so a bad approve / reject / merge is recoverable.
    "approve_draft",
    "reject_draft",
    "promote_provisional",
    "bulk_reattribute",
    "set_relationship",
    "set_warm_path_contact",
    "set_fund_inactive",
    "set_employment_status",
    "set_partner_linkedin",
    "correct_deal_attribution",
    "resolve_ambiguous_match",
    "review_attribution",
    "clear_fund_field",
    "classify_reply",
})


def stages_needing_backup() -> frozenset[str]:
    """Single source of truth for which stages get a pre-run backup."""
    return _DESTRUCTIVE_STAGES


def _backups_dir(ws_path: Path) -> Path:
    return Path(ws_path) / "backups"


def _ts() -> str:
    """Microsecond-precision UTC ISO-ish timestamp. Sub-second
    resolution matters because two pre-stage backups within the same
    second would collide on the filename + clobber each other; the
    rotation test catches that."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def backup_path_for(ws_path: Path, stage: str, ts: str) -> Path:
    """Compose the canonical backup path. Visible so the restore CLI
    can reconstruct names + so tests can assert on the layout."""
    return _backups_dir(ws_path) / f"pipeline.db.{stage}.{ts}"


def backup_before_stage(
    ws_path: Path, *, stage: str, db_path: Path,
) -> Path | None:
    """Copy the SQLite file to a stage-tagged backup. Returns the
    backup path on success, None when the stage isn't on the
    destructive list (no backup needed) or the source DB doesn't
    exist yet (first run -- nothing to back up).

    Failures (disk full, permission, race) print a WARN and return
    None rather than raising; backups are insurance, not blocking.
    """
    if stage not in _DESTRUCTIVE_STAGES:
        return None
    if not Path(db_path).exists():
        return None
    backups_dir = _backups_dir(ws_path)
    backups_dir.mkdir(parents=True, exist_ok=True)
    out = backup_path_for(ws_path, stage, _ts())
    try:
        shutil.copy2(db_path, out)
    except OSError as exc:
        print(
            f"[backups] WARN: pre-stage backup for {stage!r} failed: "
            f"{exc}. Continuing; the operator can hand-copy if needed."
        )
        return None
    _rotate(ws_path, stage)
    return out


def _rotate(ws_path: Path, stage: str) -> int:
    """Drop oldest backups for this (workspace, stage) so we keep at
    most BACKUP_KEEP_PER_STAGE. Returns count removed."""
    prefix = f"pipeline.db.{stage}."
    backups = sorted(
        (
            p for p in _backups_dir(ws_path).glob(f"{prefix}*")
            if p.is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in backups[BACKUP_KEEP_PER_STAGE:]:
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            print(
                f"[backups] WARN: rotation kept {old.name}: {exc}"
            )
    return removed


def list_backups(ws_path: Path, *, stage: str | None = None) -> list[Path]:
    """List existing backups (most recent first). Drives the restore
    CLI's selection menu + tests."""
    backups_dir = _backups_dir(ws_path)
    if not backups_dir.is_dir():
        return []
    pattern = f"pipeline.db.{stage}.*" if stage else "pipeline.db.*"
    return sorted(
        backups_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
