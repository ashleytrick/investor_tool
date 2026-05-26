"""Mint a new pipeline-batch id for cross-stage lineage (Issue #19).

Operators that want "every row this Tuesday's pipeline pass touched"
mint a batch here once, then pass `--pipeline-batch <id>` to each
stage in the run. The runs row for every stage gets stamped with the
batch id; `list_pipeline_batches.py` shows the lineage.

The batch is *optional*. Stages that don't receive --pipeline-batch
run as before with NULL pipeline_batch_id.

Examples:
  uv run scripts/new_pipeline_batch.py --workspace clients/{name}
  uv run scripts/new_pipeline_batch.py --workspace clients/{name} \\
      --notes "weekly Tuesday pass"
  # Then:
  BATCH=$(uv run scripts/new_pipeline_batch.py --workspace ... --quiet)
  uv run scripts/01_aggregate_sources.py --workspace ... --pipeline-batch $BATCH
  uv run scripts/02_enrich_funds.py --workspace ... --pipeline-batch $BATCH
  ...
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.banner import print_banner
from core.batch_ids import create_pipeline_batch
from core.config_loader import load_workspace
from core.db import get_engine


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint a new pipeline-batch id.",
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--notes", default=None,
        help="Operator-supplied reason / context recorded on the batch row.",
    )
    parser.add_argument(
        "--operator", default=None,
        help="Operator id (defaults to $USER).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Print only the batch_id (script-friendly). Without "
             "--quiet, prints the banner + a one-line summary.",
    )
    args = parser.parse_args()
    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    operator = args.operator or os.environ.get("USER") or "unknown"

    with engine.begin() as conn:
        batch_id = create_pipeline_batch(
            conn, workspace=ws.name,
            operator=operator, notes=args.notes,
        )

    if args.quiet:
        print(batch_id)
        return 0

    print_banner(ws, stage="new_pipeline_batch")
    print(f"[batch] minted: {batch_id}")
    print(f"        workspace = {ws.name}")
    print(f"        operator  = {operator}")
    print(f"        notes     = {args.notes!r}")
    print()
    print("Pass --pipeline-batch", batch_id, "to each stage in this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
