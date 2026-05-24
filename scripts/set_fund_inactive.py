"""Toggle a fund's `is_active` flag with audit logging.

Stage 6's round_fit deducts active_fund=0 when the fund is inactive, and
the major-kill aggregator escalates a partner whose fund is inactive.
Use this CLI to flip a fund's active state without raw SQL.

Examples:
  uv run scripts/set_fund_inactive.py --fund-id acme.vc \
      --reason "no new deals in 24 months"
  uv run scripts/set_fund_inactive.py --fund-id acme.vc --reactivate \
      --reason "fresh fund announced 2026-01"
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine
from core.runs import RunLogger

STAGE = "set_fund_inactive"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Toggle fund is_active flag.")
    add_workspace_arg(parser)
    parser.add_argument("--fund-id", required=True)
    parser.add_argument(
        "--reactivate", action="store_true",
        help="Set is_active=TRUE (default action is set is_active=FALSE).",
    )
    parser.add_argument("--reason", required=True,
                        help="Justification logged to run.note for audit.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    new_state = bool(args.reactivate)

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            existing = conn.execute(
                select(funds.c.fund_id, funds.c.name, funds.c.is_active)
                .where(funds.c.fund_id == args.fund_id)
            ).first()
            if not existing:
                print(f"[fund] {args.fund_id!r} not found.")
                run.failed = 1
                run.log_error(args.fund_id, "not_found", "no such fund")
                return 2
            old = existing.is_active
            conn.execute(
                funds.update()
                .where(funds.c.fund_id == args.fund_id)
                .values(is_active=new_state, last_updated=_now())
            )
        msg = (
            f"{args.fund_id} ({existing.name}): is_active {old!r} -> "
            f"{new_state!r} ({args.reason!r})"
        )
        print(f"[fund] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
