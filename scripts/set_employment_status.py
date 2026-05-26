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

from core.approval.persistence import stale_live_approvals_for_partner
from core.approval.state_machine import TRIGGER_EMPLOYMENT_LEFT_FUND
from core.config_loader import add_workspace_arg
from core.db import partners
from core.operator_command import operator_command_run

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

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            existing = conn.execute(
                select(partners.c.partner_id, partners.c.employment_status)
                .where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[employment] partner {args.partner_id!r} not found.")
                ctx.refuse("no such partner")
                run.log_error(args.partner_id, "not_found", "no such partner")
                return ctx.exit_code
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
        # The partner left the fund -- cold outreach approved against
        # their old role no longer applies. Stale every live approval
        # so the operator must re-confirm before sending.
        if args.status == "left_fund" and old != "left_fund":
            staled = stale_live_approvals_for_partner(
                engine,
                partner_id=args.partner_id,
                trigger=TRIGGER_EMPLOYMENT_LEFT_FUND,
                notes=args.reason,
            )
            if staled:
                print(
                    f"[employment] staled {staled} approved draft(s) "
                    f"for {args.partner_id} (left fund)"
                )
        run.succeeded = 1
        run.processed = 1
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
