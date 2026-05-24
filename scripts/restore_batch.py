"""Regenerate review_queue.csv from a previously-persisted Stage 7 batch.

If today's Stage 7 produced a worse batch (e.g., the LLM had a bad day),
this CLI rebuilds the CSV from an OLDER batch_id that's still in
email_drafts. Read-only against the DB; only writes the CSV.

Examples:
  uv run scripts/restore_batch.py --workspace clients/foo --list
  uv run scripts/restore_batch.py --workspace clients/foo \\
      --batch-id batch_20260301_120000
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, func, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.csv_export import write_review_queue
from core.db import (
    deck_request_responses, email_drafts, followup_drafts, funds,
    get_engine, partner_score_summaries, partners,
)


def _list_batches(engine) -> list[dict]:
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(
            select(
                email_drafts.c.batch_id,
                func.count().label("n_drafts"),
                func.min(email_drafts.c.generated_at).label("first_at"),
                func.max(email_drafts.c.generated_at).label("last_at"),
            )
            .group_by(email_drafts.c.batch_id)
            .order_by(desc("last_at"))
        ):
            out.append({
                "batch_id": r.batch_id,
                "n_drafts": r.n_drafts,
                "first_at": r.first_at,
                "last_at": r.last_at,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore review_queue.csv from a prior batch.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="List known batches with timestamps.")
    g.add_argument("--batch-id", default=None,
                   help="Restore CSV from this batch_id.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="restore_batch")

    if args.list:
        batches = _list_batches(engine)
        if not batches:
            print("[restore] no batches in email_drafts")
            return 0
        print()
        print(f"== Available batches ({len(batches)}) ==")
        for b in batches:
            print(
                f"  {b['batch_id']:30s} drafts={b['n_drafts']:4d}  "
                f"first={b['first_at']}  last={b['last_at']}"
            )
        return 0

    # Restore CSV from the given batch_id.
    batch_id = args.batch_id
    with engine.begin() as conn:
        # Pull the recommended + alternate drafts for the batch.
        recs: dict[str, dict] = {}
        alts: dict[str, dict] = {}
        for d in conn.execute(
            select(email_drafts).where(email_drafts.c.batch_id == batch_id)
            .order_by(email_drafts.c.draft_id.asc())
        ):
            target = recs if d.is_recommended else alts
            target[d.partner_id] = d
        if not recs:
            print(f"[restore] no recommended drafts in batch_id={batch_id!r}")
            return 2
        # Pull followup / deck for the same partners (latest per partner).
        followups: dict[str, str] = {}
        for f in conn.execute(
            select(followup_drafts)
            .where(followup_drafts.c.partner_id.in_(list(recs)))
            .order_by(followup_drafts.c.followup_id.asc())
        ):
            followups[f.partner_id] = f.body
        decks: dict[str, str] = {}
        for d in conn.execute(
            select(deck_request_responses)
            .where(deck_request_responses.c.partner_id.in_(list(recs)))
            .order_by(deck_request_responses.c.response_id.asc())
        ):
            decks[d.partner_id] = d.body
        # Pull partner + score context.
        contexts: dict[str, dict] = {}
        for row in conn.execute(
            select(
                partner_score_summaries,
                partners.c.name.label("partner_name"),
                partners.c.title, partners.c.linkedin_url,
                partners.c.warm_path_available,
                funds.c.name.label("fund_name"),
                funds.c.domain.label("fund_domain"),
            )
            .join(partners,
                  partners.c.partner_id == partner_score_summaries.c.partner_id)
            .join(funds, funds.c.fund_id == partners.c.fund_id)
            .where(partner_score_summaries.c.partner_id.in_(list(recs)))
        ):
            contexts[row.partner_id] = dict(row._mapping)

    # Build CSV rows.
    csv_rows: list[dict] = []
    for pid, rec in recs.items():
        ctx = contexts.get(pid, {})
        alt = alts.get(pid)
        base = {
            "partner_id": pid,
            "partner_name": ctx.get("partner_name"),
            "partner_title": ctx.get("title"),
            "fund_name": ctx.get("fund_name"),
            "fund_domain": ctx.get("fund_domain"),
            "linkedin_url": ctx.get("linkedin_url"),
            "send_now_priority": round(ctx.get("send_now_priority") or 0, 2),
            "composite_fit_score": round(ctx.get("composite_fit_score") or 0, 2),
            "round_fit_score": ctx.get("round_fit_score"),
            "round_fit_reasoning": ctx.get("round_fit_reasoning"),
            "lead_likelihood_score": ctx.get("lead_likelihood_score"),
            "lead_likelihood_signals": ctx.get("lead_likelihood_signals"),
            "cold_reachability_score": ctx.get("cold_reachability_score"),
            "spiky_belief_score": round(
                ctx.get("spiky_belief_score") or 0, 3
            ),
            "top_signals": "",
            "recommended_to_send": ctx.get("recommended_to_send"),
            "recommendation_reasoning": (
                f"RESTORED from {batch_id} via restore_batch.py. "
                f"{ctx.get('recommendation_reasoning') or ''}"
            ).strip(),
            "email_strategy_used": rec.strategy,
            "email_subject_line": rec.subject,
            "outreach_email_draft": rec.body,
            "conversion_hypothesis": rec.conversion_hypothesis or "",
            "likely_objection": rec.likely_objection or "",
            "objection_preempted": bool(rec.objection_preempted),
            "email_alternate_strategy": alt.strategy if alt else "",
            "email_draft_alternate": alt.body if alt else "",
            "followup_email_draft": followups.get(pid, ""),
            "deck_request_response": decks.get(pid, ""),
            "template_smell": rec.template_smell or "unscored",
            "warm_path_available": (
                "" if ctx.get("warm_path_available") is None
                else bool(ctx.get("warm_path_available"))
            ),
            "outreach_status": "ready_to_send"
                if ctx.get("recommended_to_send") else "draft",
        }
        csv_rows.append(base)
    out_path = write_review_queue(ws.exports_dir, csv_rows)
    print(
        f"[restore] wrote {len(csv_rows)} row(s) from batch_id={batch_id!r} "
        f"to {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
