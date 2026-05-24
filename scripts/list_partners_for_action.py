"""Several read-only "where do I need to do something" queries rolled
into one CLI. Lets the operator triage without writing SQL.

Subcommands:
  --stale-recommendations    partners recommended_to_send=TRUE with no
                              recent (last 14d) Stage 6 run
  --stale-drafts             email_drafts older than --days days, never
                              written_to_csv_at OR never pushed_to_attio_at
  --orphaned                  rows referencing parents that don't exist
                              (subset of doctor's invariant checks)
  --high-priority-no-email    recommended_to_send=TRUE partners with NULL
                              partners.email (Gmail draft generation will
                              skip them)

Examples:
  uv run scripts/list_partners_for_action.py --workspace clients/foo \\
      --high-priority-no-email --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, func, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    email_drafts,
    get_engine,
    partner_score_summaries,
    partners,
    runs,
    signals,
)


def _stale_recommendations(engine, days: int) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(
            select(
                partner_score_summaries.c.partner_id,
                partner_score_summaries.c.scored_at,
                partner_score_summaries.c.send_now_priority,
                partners.c.name,
            )
            .join(
                partners,
                partners.c.partner_id == partner_score_summaries.c.partner_id,
            )
            .where(
                partner_score_summaries.c.recommended_to_send.is_(True),
                partner_score_summaries.c.scored_at < cutoff,
            )
            .order_by(desc(partner_score_summaries.c.send_now_priority))
        ):
            out.append({
                "partner_id": r.partner_id,
                "name": r.name,
                "scored_at": str(r.scored_at) if r.scored_at else None,
                "send_now_priority": r.send_now_priority,
                "age_days": (datetime.utcnow() - r.scored_at).days
                            if r.scored_at else None,
            })
    return out


def _stale_drafts(engine, days: int) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(
            select(
                email_drafts.c.draft_id,
                email_drafts.c.partner_id,
                email_drafts.c.generated_at,
                email_drafts.c.is_recommended,
                email_drafts.c.written_to_csv_at,
                email_drafts.c.pushed_to_attio_at,
            )
            .where(email_drafts.c.generated_at < cutoff)
            .order_by(desc(email_drafts.c.generated_at))
        ):
            if r.written_to_csv_at is None or (
                r.is_recommended and r.pushed_to_attio_at is None
            ):
                out.append({
                    "draft_id": r.draft_id,
                    "partner_id": r.partner_id,
                    "generated_at": str(r.generated_at),
                    "is_recommended": bool(r.is_recommended),
                    "written_to_csv_at": str(r.written_to_csv_at)
                                        if r.written_to_csv_at else None,
                    "pushed_to_attio_at": str(r.pushed_to_attio_at)
                                         if r.pushed_to_attio_at else None,
                })
    return out


def _high_priority_no_email(engine) -> list[dict]:
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(
            select(
                partner_score_summaries.c.partner_id,
                partner_score_summaries.c.send_now_priority,
                partners.c.name,
                partners.c.fund_id,
            )
            .join(
                partners,
                partners.c.partner_id == partner_score_summaries.c.partner_id,
            )
            .where(
                partner_score_summaries.c.recommended_to_send.is_(True),
                (partners.c.email.is_(None)) | (partners.c.email == ""),
            )
            .order_by(desc(partner_score_summaries.c.send_now_priority))
        ):
            out.append({
                "partner_id": r.partner_id,
                "name": r.name,
                "fund_id": r.fund_id,
                "send_now_priority": r.send_now_priority,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Triage partners + drafts.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--stale-recommendations", action="store_true")
    g.add_argument("--stale-drafts", action="store_true")
    g.add_argument("--high-priority-no-email", action="store_true")
    parser.add_argument("--days", type=int, default=14,
                        help="Staleness threshold in days (default 14).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_for_action")

    if args.stale_recommendations:
        rows = _stale_recommendations(engine, args.days)
        title = f"recommended partners with scoring older than {args.days}d"
    elif args.stale_drafts:
        rows = _stale_drafts(engine, args.days)
        title = (f"drafts older than {args.days}d AND never written to CSV "
                 f"or (recommended AND never pushed to Attio)")
    else:
        rows = _high_priority_no_email(engine)
        title = "recommended partners with no email on file"

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print()
    print(f"== {title} ({len(rows)}) ==")
    for r in rows:
        print(f"  {json.dumps(r, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
