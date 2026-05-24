"""Apply axis_weight_suggestion rows to the workspace's axes.yaml.

Three modes:
  --list                        Show pending suggestions.
  --suggestion-id N             Apply suggestion N (single).
  --all-above CONFIDENCE        Apply every pending suggestion at confidence
                                level CONFIDENCE or higher (low/medium/high).

Backups rotate: keeps the most recent BACKUP_KEEP files (default 10).

This is the ONLY job that mutates config/axes.yaml. monthly_learning_report
only produces suggestions; routine pipeline runs never touch axes.yaml.
Running on an already-approved suggestion is a no-op.

Examples:
  uv run python jobs/apply_axis_suggestion.py --list
  uv run python jobs/apply_axis_suggestion.py --suggestion-id 3
  uv run python jobs/apply_axis_suggestion.py --all-above medium
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml
from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import axis_weight_suggestions, get_engine
from core.runs import RunLogger
from core.validate_config import preflight_or_exit

STAGE = "apply_axis_suggestion"
BACKUP_KEEP = 10  # rotate; keep this many most-recent axes.yaml backups
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rotate_backups(
    config_dir: pathlib.Path,
    keep: int = BACKUP_KEEP,
    *,
    run=None,
) -> int:
    """Keep `keep` most-recent axes.yaml.bak.* files; delete the rest.

    Batch 15 (#298): the deletion was silent -- the operator couldn't tell
    which backups had been rotated out. Now each removed file is logged
    to stdout AND to run.note (when a RunLogger is supplied) so the audit
    trail captures which historical state vanished.
    """
    backups = sorted(
        config_dir.glob("axes.yaml.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    removed_names: list[str] = []
    for old in backups[keep:]:
        try:
            name = old.name
            old.unlink()
            removed += 1
            removed_names.append(name)
        except OSError as exc:
            print(f"[apply] WARN: rotate kept {old.name}: {exc}")
    if removed_names:
        msg = (
            f"rotated {removed} backup(s) (keeping {keep} most recent): "
            f"{removed_names}"
        )
        print(f"[apply] {msg}")
        if run is not None:
            run.note(msg)
    return removed


def _apply_one(
    engine, ws, row, run, *,
    allow_already_approved: bool = False,
    approved_by: str = "unknown",
    approval_reason: str | None = None,
) -> bool:
    """Apply one suggestion row. Returns True on success."""
    axes_path = ws.config_dir / "axes.yaml"
    if row.approved and not allow_already_approved:
        print(
            f"[apply] suggestion_id={row.suggestion_id} already approved "
            f"at {row.approved_at}; skipping"
        )
        return False
    if not axes_path.exists():
        print(f"[apply] axes.yaml not found at {axes_path}")
        run.log_error(str(row.suggestion_id), "not_found", "axes.yaml missing")
        return False

    # Include microseconds + suggestion_id so --all-above can apply multiple
    # suggestions in the same wall-clock second without one backup overwriting
    # another.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = axes_path.with_name(
        f"axes.yaml.bak.{ts}.sug{row.suggestion_id}"
    )
    shutil.copy2(axes_path, backup_path)

    cfg = yaml.safe_load(axes_path.read_text(encoding="utf-8"))
    old_w = None
    for ax in cfg.get("axes", []):
        if ax["id"] == row.axis_id:
            old_w = float(ax.get("weight", 1.0))
            ax["weight"] = float(row.suggested_weight)
            break
    if old_w is None:
        backup_path.unlink(missing_ok=True)
        print(
            f"[apply] axis_id={row.axis_id!r} not present in axes.yaml; "
            "nothing applied. Backup removed."
        )
        run.log_error(row.axis_id, "axis_not_in_yaml", "missing axis")
        return False

    axes_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(
            axis_weight_suggestions.update()
            .where(axis_weight_suggestions.c.suggestion_id == row.suggestion_id)
            .values(
                approved=True, approved_at=_now(),
                approved_by=approved_by,
                approval_reason=approval_reason,
            )
        )
    print(
        f"[apply] suggestion_id={row.suggestion_id} axis {row.axis_id}: "
        f"weight {old_w} -> {row.suggested_weight} | backup={backup_path.name} "
        f"| approved_by={approved_by!r}"
    )
    run.note(
        f"applied_suggestion_id={row.suggestion_id} "
        f"axis={row.axis_id} {old_w}->{row.suggested_weight} "
        f"approved_by={approved_by!r} "
        f"reason={(approval_reason or '-')!r}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply an axis-weight suggestion.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--suggestion-id", type=int, default=None,
                   help="Apply a single suggestion by id.")
    g.add_argument("--list", action="store_true",
                   help="List pending (unapproved) suggestions and exit.")
    g.add_argument("--all-above", choices=("low", "medium", "high"),
                   help="Apply every pending suggestion at this confidence "
                        "level or higher.")
    parser.add_argument(
        "--accept-low-confidence", action="store_true",
        help="Required to apply a confidence=low suggestion. "
             "Finding 67: low-confidence suggestions are operator-discretion "
             "and shouldn't be applied silently.",
    )
    # Batch 15 #296: record who approved and why.
    parser.add_argument(
        "--approved-by", default=None,
        help="Operator identifier recorded with the approval (defaults to "
             "$USER or 'unknown' if unset).",
    )
    parser.add_argument(
        "--approval-reason", default=None,
        help="Operator rationale recorded alongside the generated reason. "
             "Optional but strongly encouraged for audit.",
    )
    args = parser.parse_args()
    import os as _os
    approver = (
        args.approved_by
        or _os.environ.get("USER")
        or _os.environ.get("USERNAME")
        or "unknown"
    )

    ws = load_workspace(args.workspace)
    preflight_or_exit(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with RunLogger(engine, ws.name, STAGE) as run:
        # ---- --list mode ----
        if args.list:
            with engine.begin() as conn:
                pending = list(conn.execute(
                    select(axis_weight_suggestions).where(
                        axis_weight_suggestions.c.approved.is_(None)
                    ).order_by(axis_weight_suggestions.c.suggestion_id)
                ))
            if not pending:
                print("[apply] no pending suggestions")
            for r in pending:
                print(
                    f"[apply] #{r.suggestion_id} axis={r.axis_id} "
                    f"{r.current_weight}->{r.suggested_weight} "
                    f"confidence={r.confidence} n={r.sample_size} "
                    f"| {r.reason}"
                )
            # Batch 15 #292: --list is a read-only listing. Recording
            # `processed = len(pending)` made every list invocation look
            # like a real apply-run in the audit, cluttering history.
            # Surface the count via run.note instead.
            run.note(f"listed {len(pending)} pending suggestion(s)")
            return 0

        # ---- --all-above mode ----
        if args.all_above:
            min_rank = CONFIDENCE_RANK[args.all_above]
            with engine.begin() as conn:
                pending = list(conn.execute(
                    select(axis_weight_suggestions).where(
                        axis_weight_suggestions.c.approved.is_(None)
                    ).order_by(axis_weight_suggestions.c.suggestion_id)
                ))
            applied = 0
            failed = 0
            for row in pending:
                run.processed += 1
                rank = CONFIDENCE_RANK.get(row.confidence, -1)
                if rank < min_rank:
                    run.skipped += 1
                    continue
                # Finding 67: even in --all-above mode, low-confidence
                # suggestions need explicit operator opt-in.
                if (
                    row.confidence == "low"
                    and not args.accept_low_confidence
                ):
                    run.skipped += 1
                    print(
                        f"[apply] skip suggestion_id={row.suggestion_id} "
                        f"(confidence=low; pass --accept-low-confidence to "
                        f"include low-confidence suggestions in --all-above)"
                    )
                    continue
                if _apply_one(
                    engine, ws, row, run,
                    approved_by=approver,
                    approval_reason=args.approval_reason,
                ):
                    applied += 1
                    run.succeeded += 1
                else:
                    failed += 1
                    run.failed += 1
            _rotate_backups(ws.config_dir, run=run)
            print(
                f"[apply] applied {applied}/{len(pending)} pending suggestions "
                f"at confidence>={args.all_above} "
                f"(failed={failed})"
            )
            # Previously this returned 0 unconditionally. Non-zero exit when
            # any application failed so cron / wrapping scripts notice.
            return 2 if failed else 0

        # ---- --suggestion-id mode (single) ----
        with engine.begin() as conn:
            row = conn.execute(
                select(axis_weight_suggestions).where(
                    axis_weight_suggestions.c.suggestion_id == args.suggestion_id
                )
            ).first()
        if not row:
            print(f"[apply] suggestion_id={args.suggestion_id} not found")
            run.failed = 1
            run.log_error(str(args.suggestion_id), "not_found",
                          "no such suggestion")
            return 2
        # Finding 67: applying a confidence=low suggestion via --suggestion-id
        # requires --accept-low-confidence so the operator can't bulk-paste
        # IDs without realizing they came from sparse data. Already-approved
        # suggestions short-circuit to no-op via _apply_one, so we skip the
        # gate in that case (re-running on an applied suggestion isn't an
        # action the operator's gating their own consent against).
        if (
            not row.approved
            and row.confidence == "low"
            and not args.accept_low_confidence
        ):
            msg = (
                f"REFUSED: suggestion_id={args.suggestion_id} has "
                f"confidence='low' (sample_size={row.sample_size}). "
                f"Re-run with --accept-low-confidence to override."
            )
            print(f"[apply] {msg}")
            run.note(msg)
            run.failed = 1
            return 2
        run.processed = 1
        if _apply_one(
            engine, ws, row, run,
            approved_by=approver,
            approval_reason=args.approval_reason,
        ):
            run.succeeded = 1
            _rotate_backups(ws.config_dir, run=run)
        elif row.approved:
            run.skipped = 1
        else:
            run.failed = 1
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
