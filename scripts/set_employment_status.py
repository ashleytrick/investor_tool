"""Mark a partner's employment status.

Common cases:
  - partner left the fund (excludes from future recommendations)
  - LinkedIn cross-check confirms they're still there
  - data is uncertain and you want to flag that

Examples:
  uv run scripts/set_employment_status.py --partner-id NAME \
      --status left_fund --reason "Twitter bio shows new role at Acme"
  uv run scripts/set_employment_status.py --partner-id NAME \
      --status verified_current --reason "LinkedIn URL still active"

Stage 6's criterion 6 ("employment_status in {likely_current,
verified_current}") respects whatever this script sets.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, partners
from core.runs import RunLogger

STAGE = "set_employment_status"

ALLOWED = {"uncertain", "likely_current", "verified_current", "left_fund"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manually set partner employment_status.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    parser.add_argument(
        "--status", required=True, choices=sorted(ALLOWED),
    )
    parser.add_argument("--reason", required=True,
                        help="Justification logged to run.note for audit.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            existing = conn.execute(
                select(partners.c.partner_id, partners.c.employment_status)
                .where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[employment] partner {args.partner_id!r} not found.")
                run.failed = 1
                run.log_error(args.partner_id, "not_found", "no such partner")
                return 2
            old = existing.employment_status
            conn.execute(
                partners.update()
                .where(partners.c.partner_id == args.partner_id)
                .values(
                    employment_status=args.status,
                    employment_verification_date=date.today(),
                    last_updated=_now(),
                )
            )
        msg = (
            f"{args.partner_id}: employment_status {old!r} -> {args.status!r} "
            f"({args.reason!r})"
        )
        print(f"[employment] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
