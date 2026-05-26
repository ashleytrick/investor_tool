"""List partners NOT recommended_to_send, grouped by which criterion failed.

Lets the operator see "we're 1 thesis quote away from recommending Alan"
or "Sofia is killed by a fund-stage mismatch we could correct."

The recommendation_reasoning column already records this in plain prose;
this CLI parses + groups it so you can prioritize where to spend signal-
collection time.

Examples:
  uv run scripts/list_blocked_recommendations.py --workspace clients/foo
  uv run scripts/list_blocked_recommendations.py --workspace clients/foo \\
      --json | jq .by_reason
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, partner_score_summaries, partners

# Criterion phrases the evaluator emits in recommendation_reasoning. Keep
# in sync with evaluate_recommended() in scripts/06_score_candidates.py.
BLOCKER_PATTERNS = (
    ("composite", "composite_fit_score below threshold"),
    ("round_fit_score", "round_fit_score below 6.0 (or disqualifier present)"),
    ("lead_likelihood_score", "lead_likelihood_score below 5.0"),
    ("fewer than 2 distinct verified",
     "<2 distinct quality>=2 evidence sources"),
    ("no verified quality>=2 evidence within last 18 months",
     "no recent verified q>=2 signal"),
    ("employment_status=", "employment not current"),
    ("major kill signal", "major kill signal present"),
    ("cold_reachability_score", "cold_reachability below 5.0"),
    # PR #10 / Slice 13 removed warm-path from the recommendation
    # gate ("no warm intros, ever"). Legacy reasoning strings on
    # pre-Slice-13 rows may still mention warm_path_available; show
    # them in their own bucket so the operator knows that bucket is
    # historical and not active gate behavior.
    ("warm_path_available=TRUE",
     "warm_path_available=TRUE (LEGACY -- pre-Slice-13 gate, no longer blocks)"),
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Group not-recommended partners by which criterion blocked them."
    )
    add_workspace_arg(parser)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON for programmatic consumption.")
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Max rows per blocker bucket in human mode (JSON returns all).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_blocked_recommendations")

    by_reason: dict[str, list[dict]] = defaultdict(list)
    unbucketed: list[dict] = []

    with engine.begin() as conn:
        rows = conn.execute(
            select(
                partner_score_summaries.c.partner_id,
                partner_score_summaries.c.recommendation_reasoning,
                partner_score_summaries.c.send_now_priority,
                partner_score_summaries.c.composite_fit_score,
                partner_score_summaries.c.round_fit_score,
                partners.c.name.label("partner_name"),
            )
            .join(
                partners,
                partners.c.partner_id == partner_score_summaries.c.partner_id,
            )
            .where(partner_score_summaries.c.recommended_to_send.is_(False))
            .order_by(desc(partner_score_summaries.c.send_now_priority))
        ).all()

    for r in rows:
        reasoning = r.recommendation_reasoning or ""
        record = {
            "partner_id": r.partner_id,
            "partner_name": r.partner_name,
            "send_now_priority": r.send_now_priority,
            "composite_fit_score": r.composite_fit_score,
            "round_fit_score": r.round_fit_score,
            "recommendation_reasoning": reasoning,
        }
        matched = False
        for pat, bucket in BLOCKER_PATTERNS:
            if pat in reasoning:
                by_reason[bucket].append(record)
                matched = True
        if not matched:
            unbucketed.append(record)

    if args.json:
        print(json.dumps({
            "by_reason": {k: v for k, v in by_reason.items()},
            "unbucketed": unbucketed,
        }, indent=2, default=str))
        return 0

    print()
    print(f"== {len(rows)} partner(s) not recommended ==")
    for bucket, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  -- {bucket} ({len(items)}) --")
        for r in items[: args.limit]:
            print(
                f"    {r['partner_id']:45s} "
                f"send_now={r['send_now_priority']:.2f if r['send_now_priority'] is not None else 'NA'}"
            )
    if unbucketed:
        print(f"\n  -- (unmatched reasoning) ({len(unbucketed)}) --")
        for r in unbucketed[: args.limit]:
            print(f"    {r['partner_id']}: {r['recommendation_reasoning'][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
