"""Resolve a Stage 3 ambiguous match by picking the right candidate.

Records the operator's pick on the ambiguous_matches row AND retroactively
fixes the deal_attributions row(s) for that source_url. Stage 3 re-runs
that produce the same ambiguity again will see the resolution and pick
the correct id without flagging again (a future enhancement; current
behavior is: the override row exists, future Stage 3 still records new
ambiguity rows for the SAME source_url but the operator can re-resolve
or use --skip-overridden once Batch 34 lands deal_attribution_overrides).

Examples:
  uv run scripts/resolve_ambiguous_match.py --workspace clients/foo \\
      --match-id 12 --resolved-id foundrynorth.example \\
      --note "Foundry North, not Foundry NorthEast (same partner)"
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.config_loader import add_workspace_arg
from core.db import (
    ambiguous_matches, deal_attributions, funds, partners,
)
from core.operator_command import operator_command_run

STAGE = "resolve_ambiguous_match"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve an ambiguous Stage 3 match.")
    add_workspace_arg(parser)
    parser.add_argument("--match-id", type=int, required=True)
    parser.add_argument(
        "--resolved-id", required=True,
        help="The chosen id (fund_id or partner_id depending on entity_type).",
    )
    parser.add_argument(
        "--resolved-by", default=None,
        help="Operator identifier (defaults to $USER).",
    )
    parser.add_argument(
        "--note", default=None,
        help="Free-text rationale recorded with the resolution.",
    )
    args = parser.parse_args()
    resolver = (
        args.resolved_by or os.environ.get("USER") or "unknown"
    )

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            row = conn.execute(
                select(ambiguous_matches).where(
                    ambiguous_matches.c.match_id == args.match_id,
                )
            ).first()
            if not row:
                print(f"[resolve] match_id={args.match_id} not found")
                run.failed = 1
                run.log_error(str(args.match_id), "not_found",
                              "no such ambiguous_matches row")
                return 2
            # Validate that resolved_id exists in the right table.
            if row.entity_type == "fund":
                exists = conn.execute(
                    select(funds.c.fund_id).where(
                        funds.c.fund_id == args.resolved_id,
                    )
                ).first()
            elif row.entity_type == "partner":
                exists = conn.execute(
                    select(partners.c.partner_id).where(
                        partners.c.partner_id == args.resolved_id,
                    )
                ).first()
            else:
                exists = None
            if not exists:
                print(
                    f"[resolve] resolved_id={args.resolved_id!r} not in "
                    f"{row.entity_type}s table; refuse to point at a "
                    f"non-existent row."
                )
                run.failed = 1
                run.log_error(args.resolved_id, "not_found",
                              f"{row.entity_type} id not in DB")
                return 2
            conn.execute(
                ambiguous_matches.update()
                .where(ambiguous_matches.c.match_id == args.match_id)
                .values(
                    resolved_id=args.resolved_id,
                    resolved_at=_now(),
                    resolved_by=resolver,
                    resolution_note=args.note,
                )
            )
            # Retroactively fix deal_attributions for the same source_url.
            if row.entity_type == "fund":
                conn.execute(
                    deal_attributions.update()
                    .where(
                        deal_attributions.c.source_url == row.source_url,
                        deal_attributions.c.lead_fund_id == row.chosen_id,
                    )
                    .values(lead_fund_id=args.resolved_id)
                )
        msg = (
            f"match_id={args.match_id}: "
            f"{row.entity_type} {row.raw_name!r} resolved to "
            f"{args.resolved_id!r} by {resolver!r}; deal_attributions for "
            f"source_url={row.source_url!r} updated."
        )
        print(f"[resolve] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
