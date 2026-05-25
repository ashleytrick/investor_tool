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

from core.config_loader import add_workspace_arg
from core.db import funds
from core.operator_command import operator_command_run

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

    new_state = bool(args.reactivate)

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            existing = conn.execute(
                select(funds.c.fund_id, funds.c.name, funds.c.is_active)
                .where(funds.c.fund_id == args.fund_id)
            ).first()
            if not existing:
                print(f"[fund] {args.fund_id!r} not found.")
                ctx.refuse("no such fund")
                run.log_error(args.fund_id, "not_found", "no such fund")
                return ctx.exit_code
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
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
