"""Manually clear (set to NULL) a fund field that's gone stale, with
audit logging.

Stage 2's enrichment preserves richer prior values when a re-run
produces a sparse extraction (Batch 11 #412/#413). But that means
stale facts can persist forever if the operator wants them gone -- a
fund's old `kill_signals` text, a deprecated `stated_thesis`, etc.
This CLI is the supported way to drop them without raw SQL.

Examples:
  uv run scripts/clear_fund_field.py --workspace clients/foo \\
      --fund-id acme.vc --field kill_signals \\
      --reason "old text outdated; pre-fund-II"
  uv run scripts/clear_fund_field.py --workspace clients/foo \\
      --fund-id acme.vc --field stated_thesis \\
      --reason "they pivoted; let Stage 2 re-extract"
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine
from core.runs import RunLogger

STAGE = "clear_fund_field"

# Whitelist of fields we'll clear. Excludes structural fields like
# fund_id, name (required), attio_record_id (sync state). Includes any
# Stage 2-populated content the operator may want to retire.
CLEARABLE_FIELDS = {
    "stated_thesis", "stated_stage_focus", "check_size_range",
    "kill_signals", "source_urls",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear a fund field with audit.")
    add_workspace_arg(parser)
    parser.add_argument("--fund-id", required=True)
    parser.add_argument("--field", required=True, choices=sorted(CLEARABLE_FIELDS))
    parser.add_argument("--reason", required=True)
    parser.add_argument("--created-by", default=None,
                        help="Defaults to $USER.")
    args = parser.parse_args()
    operator = args.created_by or os.environ.get("USER") or "unknown"

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            row = conn.execute(
                select(funds.c.fund_id, funds.c.name, funds.c[args.field])
                .where(funds.c.fund_id == args.fund_id)
            ).first()
            if not row:
                print(f"[clear] fund {args.fund_id!r} not found")
                run.failed = 1
                run.log_error(args.fund_id, "not_found", "no such fund")
                return 2
            old_value = getattr(row, args.field)
            conn.execute(
                funds.update().where(funds.c.fund_id == args.fund_id)
                .values(**{args.field: None}, last_updated=_now())
            )
        msg = (
            f"{args.fund_id} ({row.name}): cleared {args.field!r} "
            f"(was {old_value!r}) by {operator!r}: {args.reason!r}"
        )
        print(f"[clear] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
