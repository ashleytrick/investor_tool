"""Attach / correct a partner's LinkedIn URL.

Stage 5 verification uses the LinkedIn URL as a hint for partner identity
when verifying quotes, and Stage 8 sync uses it as a match key. Stage 4
fixture parsers don't always populate it. This CLI lets the operator
patch it without raw SQL.

Examples:
  uv run scripts/set_partner_linkedin.py --partner-id NAME \
      --url https://www.linkedin.com/in/priya-anand
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.config_loader import add_workspace_arg
from core.db import partners
from core.operator_command import operator_command_run

STAGE = "set_partner_linkedin"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set or clear partner LinkedIn URL.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="LinkedIn URL to attach.")
    g.add_argument("--clear", action="store_true",
                   help="Clear the existing LinkedIn URL.")
    args = parser.parse_args()

    if args.url and not (
        args.url.startswith("http://") or args.url.startswith("https://")
    ):
        print(
            f"[linkedin] URL must start with http:// or https://; got {args.url!r}"
        )
        return 2

    new_url = None if args.clear else args.url

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            existing = conn.execute(
                select(partners.c.partner_id, partners.c.linkedin_url)
                .where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[linkedin] partner {args.partner_id!r} not found.")
                ctx.refuse("no such partner")
                run.log_error(args.partner_id, "not_found", "no such partner")
                return ctx.exit_code
            conn.execute(
                partners.update()
                .where(partners.c.partner_id == args.partner_id)
                .values(linkedin_url=new_url, last_updated=_now())
            )
        msg = (
            f"{args.partner_id}: linkedin_url {existing.linkedin_url!r} -> "
            f"{new_url!r}"
        )
        print(f"[linkedin] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
