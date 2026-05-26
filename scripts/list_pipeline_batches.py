"""List pipeline batches + the runs linked to each (Issue #19).

Once an operator has minted a batch via scripts/new_pipeline_batch.py
and passed `--pipeline-batch <id>` to a sequence of stages, every
runs row for those stages stamps pipeline_batch_id. This CLI surfaces
that lineage: each batch shows its workspace, when it started /
ended, who opened it, and the runs (stage + run_id + status) that
were part of it.

Examples:
  uv run scripts/list_pipeline_batches.py --workspace clients/{name}
  uv run scripts/list_pipeline_batches.py --workspace clients/{name} \\
      --batch-id <id>
  uv run scripts/list_pipeline_batches.py --workspace clients/{name} --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, pipeline_batches, runs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List pipeline batches + their stage runs.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--batch-id", default=None,
        help="Show details for a single batch only.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_pipeline_batches")

    with engine.begin() as conn:
        batch_stmt = select(pipeline_batches).where(
            pipeline_batches.c.workspace == ws.name,
        )
        if args.batch_id:
            batch_stmt = batch_stmt.where(
                pipeline_batches.c.batch_id == args.batch_id,
            )
        batch_stmt = batch_stmt.order_by(
            desc(pipeline_batches.c.started_at),
        )
        batch_rows = list(conn.execute(batch_stmt))
        runs_by_batch: dict[str, list] = {}
        for r in conn.execute(
            select(
                runs.c.run_id, runs.c.stage, runs.c.started_at,
                runs.c.completed_at, runs.c.records_processed,
                runs.c.records_failed, runs.c.pipeline_batch_id,
            ).where(
                runs.c.pipeline_batch_id.isnot(None),
                runs.c.workspace == ws.name,
            ).order_by(runs.c.run_id)
        ):
            runs_by_batch.setdefault(r.pipeline_batch_id, []).append(r)

    if args.json:
        print(json.dumps([
            {
                "batch_id": b.batch_id,
                "workspace": b.workspace,
                "operator": b.operator,
                "notes": b.notes,
                "started_at": b.started_at.isoformat() if b.started_at else None,
                "completed_at": b.completed_at.isoformat() if b.completed_at else None,
                "runs": [
                    {
                        "run_id": r.run_id,
                        "stage": r.stage,
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                        "records_processed": r.records_processed,
                        "records_failed": r.records_failed,
                    }
                    for r in runs_by_batch.get(b.batch_id, [])
                ],
            }
            for b in batch_rows
        ], indent=2))
        return 0

    if not batch_rows:
        print("[batches] no pipeline batches recorded in this workspace")
        return 0
    print(f"== {len(batch_rows)} pipeline batch(es) for {ws.name} ==")
    for b in batch_rows:
        rs = runs_by_batch.get(b.batch_id, [])
        ended = b.completed_at.isoformat() if b.completed_at else "OPEN"
        print(
            f"\n  {b.batch_id}  operator={b.operator!r}  "
            f"started={b.started_at.isoformat() if b.started_at else '?'}  "
            f"ended={ended}"
        )
        if b.notes:
            print(f"    notes: {b.notes}")
        if not rs:
            print("    (no runs linked yet)")
        for r in rs:
            failed_tag = f"  FAILED={r.records_failed}" if (r.records_failed or 0) > 0 else ""
            print(
                f"    run_id={r.run_id:>4}  stage={r.stage:<26}  "
                f"processed={r.records_processed!r}{failed_tag}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
