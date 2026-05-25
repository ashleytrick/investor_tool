"""Gate 5.5 calibration as discipline-as-code.

The brief mandates that before scaling to the top 25 partners, the operator
sends an 8-10 partner calibration batch to MID-priority targets (ranks 5-15
by send_now_priority), waits 5+ business days for outcomes, scores the
result green/yellow/red, and only proceeds to top-25 on Green.

Today this is procedural; this script makes it mechanical:
  - `--start [--n 10]`: pick a mid-priority cohort and persist it.
  - `--status`: show pending + recent cohorts and their outcomes.
  - `--complete --outcome green|yellow|red --reason "..."`: score the latest
    pending cohort.

Stage 7 reads the latest cohort: if --top > 10 AND no Green calibration in
the last 60 days, Stage 7 refuses without --skip-calibration --reason "...".
That keeps the brief's most important quality gate intact without slowing
down the early calibration runs themselves.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    calibration_cohorts,
    get_engine,
    outcomes,
    partner_score_summaries,
    partners,
)
from core.runs import RunLogger

STAGE = "calibration"
CALIBRATION_WINDOW_DAYS = 60
MID_TIER_RANK_START = 5  # 1-indexed; skip the top 4 partners
MID_TIER_RANK_END = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def latest_green(engine, *, window_days: int = CALIBRATION_WINDOW_DAYS):
    """Return the most recent Green calibration cohort within window, else None."""
    cutoff = _now() - timedelta(days=window_days)
    with engine.begin() as conn:
        return conn.execute(
            select(calibration_cohorts).where(
                calibration_cohorts.c.outcome == "green",
                calibration_cohorts.c.completed_at >= cutoff,
            ).order_by(desc(calibration_cohorts.c.completed_at)).limit(1)
        ).first()


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate 5.5 calibration batch.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true",
                   help="Select a new mid-priority calibration cohort.")
    g.add_argument("--status", action="store_true",
                   help="Show calibration cohorts in flight + recent outcomes.")
    g.add_argument("--complete", action="store_true",
                   help="Mark the latest pending cohort with an outcome.")
    parser.add_argument("--n", type=int, default=10,
                        help="Cohort size for --start (default 10).")
    parser.add_argument("--outcome", choices=("green", "yellow", "red"),
                        help="Required with --complete.")
    parser.add_argument("--reason", default=None,
                        help="Justification for --complete (required).")
    args = parser.parse_args()

    if args.complete and (not args.outcome or not args.reason):
        parser.error("--complete requires --outcome and --reason")

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with RunLogger(engine, ws.name, STAGE) as run:
        if args.status:
            with engine.begin() as conn:
                rows = list(conn.execute(
                    select(calibration_cohorts).order_by(
                        desc(calibration_cohorts.c.cohort_id)
                    ).limit(10)
                ))
            if not rows:
                print("[calibration] no cohorts yet")
                run.note("no cohorts")
                return 0
            for r in rows:
                pids = json.loads(r.partner_ids or "[]")
                state = r.outcome or "PENDING"
                print(
                    f"[calibration] cohort #{r.cohort_id} ({state}) "
                    f"n={len(pids)} started={r.started_at} "
                    f"completed={r.completed_at or '-'} "
                    f"reason={r.reason or '-'}"
                )
            run.processed = len(rows)
            run.succeeded = run.processed
            return 0

        if args.start:
            # Pull current recommended partners ranked by send_now_priority,
            # skip the top (MID_TIER_RANK_START - 1), take up to --n from the
            # mid tier.
            with engine.begin() as conn:
                ranked = list(conn.execute(
                    select(
                        partner_score_summaries.c.partner_id,
                        partner_score_summaries.c.send_now_priority,
                        partners.c.name,
                    )
                    .join(partners,
                          partners.c.partner_id == partner_score_summaries.c.partner_id)
                    .where(
                        partner_score_summaries.c.recommended_to_send.is_(True)
                    )
                    .order_by(
                        desc(partner_score_summaries.c.send_now_priority)
                    )
                ))
            if len(ranked) < MID_TIER_RANK_START:
                print(
                    f"[calibration] only {len(ranked)} recommended partners; "
                    f"need at least {MID_TIER_RANK_START} to skip the top tier"
                )
                run.failed = 1
                return 2
            mid = ranked[MID_TIER_RANK_START - 1: MID_TIER_RANK_END]
            cohort = mid[: args.n]
            if not cohort:
                print("[calibration] no mid-priority partners available")
                run.failed = 1
                return 2
            partner_ids = [r.partner_id for r in cohort]
            with engine.begin() as conn:
                result = conn.execute(calibration_cohorts.insert().values(
                    started_at=_now(),
                    partner_ids=json.dumps(partner_ids),
                ))
                cohort_id = int(result.inserted_primary_key[0])
            print(f"[calibration] started cohort #{cohort_id} with {len(cohort)} partner(s):")
            for r in cohort:
                print(f"  {r.partner_id:50s} send_now={r.send_now_priority:.2f}  {r.name}")
            print()
            print(
                "Next: send these emails (review their drafts in review_queue.csv "
                "or via Gmail drafts), wait 5+ business days, then run:"
            )
            print("  uv run scripts/calibration.py --complete "
                  "--outcome green|yellow|red --reason \"...\"")
            run.processed = len(cohort)
            run.succeeded = run.processed
            run.note(f"cohort_id={cohort_id} partners={partner_ids}")
            return 0

        # --complete
        with engine.begin() as conn:
            pending = conn.execute(
                select(calibration_cohorts).where(
                    calibration_cohorts.c.outcome.is_(None)
                ).order_by(desc(calibration_cohorts.c.cohort_id)).limit(1)
            ).first()
        if not pending:
            print("[calibration] no pending cohort to complete")
            run.failed = 1
            return 2
        with engine.begin() as conn:
            conn.execute(
                calibration_cohorts.update()
                .where(calibration_cohorts.c.cohort_id == pending.cohort_id)
                .values(
                    outcome=args.outcome,
                    reason=args.reason,
                    completed_at=_now(),
                )
            )
        run.succeeded = 1
        run.note(f"cohort_id={pending.cohort_id} outcome={args.outcome}")
        print(
            f"[calibration] cohort #{pending.cohort_id} marked {args.outcome!r}. "
            f"reason={args.reason!r}"
        )
        # Quick stats: how many outcomes the operator recorded for these partners?
        partner_ids = json.loads(pending.partner_ids or "[]")
        if partner_ids:
            with engine.begin() as conn:
                replied = conn.execute(
                    select(outcomes.c.partner_id).where(
                        outcomes.c.partner_id.in_(partner_ids)
                    )
                ).all()
            print(
                f"[calibration] outcomes recorded for {len({r.partner_id for r in replied})}"
                f"/{len(partner_ids)} cohort partners"
            )
        if args.outcome != "green":
            print(
                "[calibration] not Green: revise prompts/examples/, "
                "company.yaml round_hook / strongest_raise_proof, OR the "
                "generate_email prompt. Re-run a fresh calibration batch on "
                "different mid-priority partners before scaling to top-25."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
