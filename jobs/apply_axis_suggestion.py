"""Apply a specific axis_weight_suggestion to the workspace's axes.yaml.

Reads the row from axis_weight_suggestions, backs up the current axes.yaml as
axes.yaml.bak.<unix-ts>, updates the target axis's `weight`, writes back, and
marks the suggestion `approved=True` with `approved_at=<now>`.

This is the ONLY job that mutates config/axes.yaml. monthly_learning_report
only produces suggestions; routine pipeline runs never touch axes.yaml.
Idempotent in the sense that running on an already-approved suggestion does
nothing and exits 0 with a skip message.

Run: uv run python jobs/apply_axis_suggestion.py \
       --workspace clients/{name} --suggestion-id 1
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

from core.config_loader import add_workspace_arg, load_workspace
from core.db import axis_weight_suggestions, get_engine
from core.runs import RunLogger

STAGE = "apply_axis_suggestion"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply an axis-weight suggestion.")
    add_workspace_arg(parser)
    parser.add_argument("--suggestion-id", type=int, required=True)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            row = conn.execute(
                select(axis_weight_suggestions).where(
                    axis_weight_suggestions.c.suggestion_id == args.suggestion_id
                )
            ).first()
        if not row:
            print(f"[apply] suggestion_id={args.suggestion_id} not found")
            run.failed = 1
            run.log_error(str(args.suggestion_id), "not_found", "no such suggestion")
            return 2
        if row.approved:
            print(
                f"[apply] suggestion_id={args.suggestion_id} already approved "
                f"at {row.approved_at}; nothing to do"
            )
            run.skipped = 1
            return 0

        axes_path = ws.config_dir / "axes.yaml"
        if not axes_path.exists():
            print(f"[apply] axes.yaml not found at {axes_path}")
            run.failed = 1
            return 2

        ts = int(datetime.now().timestamp())
        backup_path = axes_path.with_name(f"axes.yaml.bak.{ts}")
        shutil.copy2(axes_path, backup_path)

        cfg = yaml.safe_load(axes_path.read_text(encoding="utf-8"))
        updated = False
        for ax in cfg.get("axes", []):
            if ax["id"] == row.axis_id:
                old_w = float(ax.get("weight", 1.0))
                ax["weight"] = float(row.suggested_weight)
                updated = True
                break
        if not updated:
            backup_path.unlink(missing_ok=True)
            print(
                f"[apply] axis_id={row.axis_id!r} not present in axes.yaml; "
                "nothing applied. Backup removed."
            )
            run.failed = 1
            run.log_error(row.axis_id, "axis_not_in_yaml",
                          "suggestion targets an axis not in current axes.yaml")
            return 2

        axes_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        with engine.begin() as conn:
            conn.execute(
                axis_weight_suggestions.update()
                .where(axis_weight_suggestions.c.suggestion_id == args.suggestion_id)
                .values(approved=True, approved_at=_now())
            )
        print(
            f"[apply] axis {row.axis_id}: weight {old_w} -> "
            f"{row.suggested_weight}. Backup: {backup_path.name}"
        )
        run.succeeded = 1
        run.note(
            f"applied_suggestion_id={args.suggestion_id} "
            f"axis={row.axis_id} {old_w}->{row.suggested_weight}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
