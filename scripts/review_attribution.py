"""Operator CLI for the ambiguous-attribution review queue (Slice 6).

Lists pending ambiguous attributions + lets the operator confirm,
reject, or dismiss them. A confirmation rewrites the chosen
deal_attributions row's match_status to `confirmed` so Stage 6's
filter starts counting it.

Usage:
  uv run scripts/review_attribution.py --workspace clients/{name}
  uv run scripts/review_attribution.py --workspace clients/{name} \\
      --review-id 7 --confirm
  uv run scripts/review_attribution.py --workspace clients/{name} \\
      --review-id 7 --reject --reason "wrong fund"
  uv run scripts/review_attribution.py --workspace clients/{name} \\
      --review-id 7 --dismiss --reason "low signal source"

Confirm: the chosen attribution stands; match_status flips to
        `confirmed`, matched_by stays whatever Stage 3 set.
Reject:  the chosen attribution is wrong; match_status flips to
        `rejected` so Stage 6 ignores it forever.
Dismiss: the review item is closed without changing the
        deal_attributions row (useful when the operator decides
        the source URL itself isn't worth chasing).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import update

from core.attribution.review_queue import (
    KIND_AMBIGUOUS_ATTRIBUTION, list_pending, resolve,
)
from core.attribution.status import (
    MATCHED_BY_MANUAL, STATUS_CONFIRMED, STATUS_REJECTED,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import deal_attributions, get_engine


def _actor(cli: str | None) -> str:
    return cli or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve ambiguous attribution review items.",
    )
    add_workspace_arg(parser)
    parser.add_argument("--review-id", type=int, default=None)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--confirm", action="store_true")
    action.add_argument("--reject", action="store_true")
    action.add_argument("--dismiss", action="store_true")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--reviewed-by", default=None)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="review_attribution")
    actor = _actor(args.reviewed_by)

    if args.review_id is None:
        pending = list_pending(engine, kind=KIND_AMBIGUOUS_ATTRIBUTION)
        if not pending:
            print("[review_attribution] no pending ambiguous attributions")
            return 0
        print(
            f"[review_attribution] {len(pending)} pending. "
            f"Use --review-id N --confirm/--reject/--dismiss to act."
        )
        for r in pending:
            ctx = {}
            try:
                ctx = json.loads(r.context or "{}")
            except Exception:  # noqa: BLE001
                pass
            print(
                f"\n  review_id={r.review_id}  target={r.target_id}\n"
                f"    raw_lead: {ctx.get('raw_lead_investor', '?')}\n"
                f"    chosen:   {ctx.get('chosen_fund_id', '?')}\n"
                f"    candidates: {ctx.get('candidates', [])}"
            )
        return 0

    if not (args.confirm or args.reject or args.dismiss):
        print(
            "[review_attribution] one of --confirm / --reject / "
            "--dismiss is required with --review-id"
        )
        return 1

    # Look up the review row to get its target (source_url).
    pending = list_pending(engine, kind=KIND_AMBIGUOUS_ATTRIBUTION)
    target = None
    for r in pending:
        if r.review_id == args.review_id:
            target = r
            break
    if target is None:
        print(
            f"[review_attribution] review_id={args.review_id} not "
            f"pending (may already be resolved)"
        )
        return 1

    if args.confirm:
        with engine.begin() as conn:
            conn.execute(
                update(deal_attributions)
                .where(deal_attributions.c.source_url == target.target_id)
                .values(
                    match_status=STATUS_CONFIRMED,
                    matched_by=MATCHED_BY_MANUAL,
                    review_status=STATUS_CONFIRMED,
                    reviewed_by=actor,
                    reviewed_at=_now(),
                )
            )
        resolve(engine, review_id=args.review_id, resolved_by=actor,
                notes=args.reason or "confirmed")
        print(
            f"[review_attribution] confirmed source={target.target_id} "
            f"by {actor}"
        )
        return 0

    if args.reject:
        if not args.reason:
            print("[review_attribution] --reject requires --reason")
            return 1
        with engine.begin() as conn:
            conn.execute(
                update(deal_attributions)
                .where(deal_attributions.c.source_url == target.target_id)
                .values(
                    match_status=STATUS_REJECTED,
                    review_status=STATUS_REJECTED,
                    reviewed_by=actor,
                    reviewed_at=_now(),
                )
            )
        resolve(engine, review_id=args.review_id, resolved_by=actor,
                notes=args.reason)
        print(
            f"[review_attribution] rejected source={target.target_id} "
            f"by {actor}: {args.reason}"
        )
        return 0

    if args.dismiss:
        resolve(engine, review_id=args.review_id, resolved_by=actor,
                notes=args.reason or "dismissed", status="dismissed")
        print(
            f"[review_attribution] dismissed review_id={args.review_id}"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
