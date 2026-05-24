"""Operator override for a Stage 3 attribution.

Use cases:
  - Stage 3 attributed a deal to the wrong fund (--action set with --fund-id)
  - Stage 3 attributed a deal to the wrong partner (--action set with --partner-id)
  - The announcement isn't actually a funding event (--action reject)
  - Note for future auditors (--action note --reason "...")

Overrides are keyed on source_url (one announcement -> one override) and
PRESERVED across Stage 3 re-runs so the LLM can't silently reintroduce
the wrong attribution.

Examples:
  uv run scripts/correct_deal_attribution.py --workspace clients/foo \\
      --source-url https://techcrunch.com/2026/01/15/acme-seed \\
      --action set --fund-id northbeam.example \\
      --partner-id northbeam.example_priya_anand \\
      --reason "Stage 3 picked Northeast; it was Priya at Northbeam."
  uv run scripts/correct_deal_attribution.py --workspace clients/foo \\
      --source-url URL --action reject --reason "not actually funding news"
  uv run scripts/correct_deal_attribution.py --workspace clients/foo \\
      --source-url URL --action note --reason "double-check at next batch"
  uv run scripts/correct_deal_attribution.py --workspace clients/foo \\
      --source-url URL --clear
  uv run scripts/correct_deal_attribution.py --workspace clients/foo --list
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    deal_attribution_overrides, deal_attributions, funds, get_engine,
    partners,
)
from core.runs import RunLogger

STAGE = "correct_deal_attribution"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Override Stage 3 attribution.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="List active overrides.")
    g.add_argument("--clear", action="store_true",
                   help="Drop the override on --source-url.")
    g.add_argument("--action", choices=("reject", "set", "note"),
                   help="Override action.")
    parser.add_argument("--source-url", default=None,
                        help="Required for non-list ops.")
    parser.add_argument("--fund-id", default=None,
                        help="With --action set: forces lead_fund_id.")
    parser.add_argument("--partner-id", default=None,
                        help="With --action set: forces attributed_partner_id.")
    parser.add_argument("--reason", default=None,
                        help="Required for non-list / non-clear ops.")
    parser.add_argument(
        "--created-by", default=None,
        help="Operator identifier (defaults to $USER).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    operator = args.created_by or os.environ.get("USER") or "unknown"

    if args.list:
        with engine.begin() as conn:
            rows = list(conn.execute(
                select(deal_attribution_overrides)
                .order_by(desc(deal_attribution_overrides.c.override_id))
            ))
        print()
        print(f"== deal_attribution_overrides ({len(rows)}) ==")
        for r in rows:
            print(
                f"  #{r.override_id:4d} {r.action:6s} url={r.source_url!r} "
                f"fund={r.lead_fund_id!r} partner={r.attributed_partner_id!r} "
                f"by={r.created_by!r} at={r.created_at} reason={r.reason!r}"
            )
        return 0

    if not args.source_url:
        parser.error("--source-url is required unless --list")

    with RunLogger(engine, ws.name, STAGE) as run:
        if args.clear:
            with engine.begin() as conn:
                deleted = conn.execute(
                    deal_attribution_overrides.delete().where(
                        deal_attribution_overrides.c.source_url == args.source_url,
                    )
                ).rowcount
            if not deleted:
                print(f"[override] no override on {args.source_url!r}")
                run.skipped = 1
                return 0
            print(f"[override] cleared override on {args.source_url!r}")
            run.note(f"cleared override for {args.source_url}")
            run.succeeded = 1
            return 0

        if not args.reason:
            parser.error("--reason is required for set/reject/note actions")
        # Validate referenced ids exist (set action).
        if args.action == "set":
            if not (args.fund_id or args.partner_id):
                parser.error(
                    "--action set requires --fund-id and/or --partner-id"
                )
            with engine.begin() as conn:
                if args.fund_id:
                    if not conn.execute(
                        select(funds.c.fund_id).where(
                            funds.c.fund_id == args.fund_id,
                        )
                    ).first():
                        print(f"[override] fund_id={args.fund_id!r} not in DB")
                        run.failed = 1
                        run.log_error(args.fund_id, "not_found", "no such fund")
                        return 2
                if args.partner_id:
                    if not conn.execute(
                        select(partners.c.partner_id).where(
                            partners.c.partner_id == args.partner_id,
                        )
                    ).first():
                        print(
                            f"[override] partner_id={args.partner_id!r} not in DB"
                        )
                        run.failed = 1
                        run.log_error(args.partner_id, "not_found",
                                      "no such partner")
                        return 2
        # Upsert: delete existing override on this source_url first.
        with engine.begin() as conn:
            conn.execute(
                deal_attribution_overrides.delete().where(
                    deal_attribution_overrides.c.source_url == args.source_url,
                )
            )
            conn.execute(deal_attribution_overrides.insert().values(
                source_url=args.source_url,
                action=args.action,
                lead_fund_id=args.fund_id,
                attributed_partner_id=args.partner_id,
                reason=args.reason,
                created_by=operator,
                created_at=_now(),
            ))
            # Apply RIGHT NOW so the operator sees the effect without
            # waiting for the next Stage 3 run.
            if args.action == "reject":
                # Wipe existing matched-attribution rows for this URL;
                # leave the skeleton (raw_lead_investor present) so the
                # audit trail survives.
                conn.execute(
                    deal_attributions.update()
                    .where(deal_attributions.c.source_url == args.source_url)
                    .values(
                        lead_fund_id=None,
                        attributed_partner_id=None,
                    )
                )
            elif args.action == "set":
                updates = {}
                if args.fund_id:
                    updates["lead_fund_id"] = args.fund_id
                if args.partner_id:
                    updates["attributed_partner_id"] = args.partner_id
                if updates:
                    conn.execute(
                        deal_attributions.update()
                        .where(deal_attributions.c.source_url == args.source_url)
                        .values(**updates)
                    )
        msg = (
            f"{args.action}: {args.source_url!r} fund={args.fund_id!r} "
            f"partner={args.partner_id!r} reason={args.reason!r} by={operator!r}"
        )
        print(f"[override] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
