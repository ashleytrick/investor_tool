"""Single-pane status view: counts, last runs, pending decisions, errors,
and the recommended next command.

Run: uv run scripts/status.py [--workspace clients/foo]
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
from core.validate_config import validate_workspace_config
from core.db import (
    axis_weight_suggestions,
    deal_attributions,
    email_drafts,
    funds,
    get_engine,
    outcomes,
    partner_score_summaries,
    partners,
    run_errors,
    runs,
    signals,
    source_snapshots,
)


def _fmt_ts(ts) -> str:
    if ts is None:
        return "never"
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline status.")
    add_workspace_arg(parser)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="status")
    # status.py never refuses -- the operator runs it to diagnose, so surface
    # validation issues as warnings instead of an exit-2.
    config_issues = validate_workspace_config(ws)
    if config_issues:
        print(f"\n[status] CONFIG WARNINGS ({len(config_issues)}):")
        for s in config_issues:
            print(f"  - {s}")
    csv_path = ws.exports_dir / "review_queue.csv"

    with engine.begin() as conn:
        # Counts.
        n_funds = conn.execute(select(func.count()).select_from(funds)).scalar()
        n_active_funds = conn.execute(
            select(func.count()).select_from(funds).where(funds.c.is_active.is_(True))
        ).scalar()
        n_partners = conn.execute(select(func.count()).select_from(partners)).scalar()
        n_partners_likely = conn.execute(
            select(func.count()).select_from(partners).where(
                partners.c.employment_status.in_(("likely_current", "verified_current"))
            )
        ).scalar()
        n_warm = conn.execute(
            select(func.count()).select_from(partners).where(
                partners.c.warm_path_available.is_(True)
            )
        ).scalar()
        n_signals = conn.execute(select(func.count()).select_from(signals)).scalar()
        n_verified = conn.execute(
            select(func.count()).select_from(signals).where(
                signals.c.verified.is_(True)
            )
        ).scalar()
        n_q2 = conn.execute(
            select(func.count()).select_from(signals).where(
                signals.c.signal_quality_score >= 2
            )
        ).scalar()
        n_q3 = conn.execute(
            select(func.count()).select_from(signals).where(
                signals.c.signal_quality_score >= 3
            )
        ).scalar()
        n_deals = conn.execute(
            select(func.count()).select_from(deal_attributions)
        ).scalar()
        n_summaries = conn.execute(
            select(func.count()).select_from(partner_score_summaries)
        ).scalar()
        n_recommended = conn.execute(
            select(func.count()).select_from(partner_score_summaries).where(
                partner_score_summaries.c.recommended_to_send.is_(True)
            )
        ).scalar()
        n_score_override = conn.execute(
            select(func.count()).select_from(partner_score_summaries).where(
                partner_score_summaries.c.manual_score_override.is_(True)
            )
        ).scalar()
        n_rec_override = conn.execute(
            select(func.count()).select_from(partner_score_summaries).where(
                partner_score_summaries.c.manual_recommended_override.is_(True)
            )
        ).scalar()
        n_drafts = conn.execute(
            select(func.count()).select_from(email_drafts)
        ).scalar()
        n_snapshots = conn.execute(
            select(func.count()).select_from(source_snapshots)
        ).scalar()
        n_outcomes = conn.execute(select(func.count()).select_from(outcomes)).scalar()
        n_pending_suggestions = conn.execute(
            select(func.count()).select_from(axis_weight_suggestions).where(
                axis_weight_suggestions.c.approved.is_(None)
            )
        ).scalar()

        # Last run per stage (latest run_id wins; carries processed counts so
        # status surfaces "ran but ingested zero" as a yellow flag).
        last_by_stage: dict[str, object] = {}
        for r in conn.execute(
            select(
                runs.c.stage, runs.c.run_id, runs.c.completed_at,
                runs.c.records_processed, runs.c.records_succeeded,
                runs.c.records_failed, runs.c.records_skipped,
                runs.c.error_summary,
            ).order_by(desc(runs.c.run_id))
        ):
            if r.stage not in last_by_stage:
                last_by_stage[r.stage] = r

        # Recent errors.
        recent_errors = list(conn.execute(
            select(
                run_errors.c.occurred_at,
                run_errors.c.record_id,
                run_errors.c.error_type,
                run_errors.c.error_message,
            ).order_by(desc(run_errors.c.error_id)).limit(5)
        ))

    print()
    print("== Pipeline counts ==")
    print(f"  funds:                  {n_funds} (active: {n_active_funds})")
    print(f"  partners:               {n_partners} "
          f"(employment current/likely: {n_partners_likely}, warm path: {n_warm})")
    print(f"  signals:                {n_signals} "
          f"(verified: {n_verified}, quality>=2: {n_q2}, quality>=3: {n_q3})")
    print(f"  deal_attributions:      {n_deals}")
    print(f"  source_snapshots:       {n_snapshots}")
    print(f"  partner_score_summaries:{n_summaries} "
          f"(recommended_to_send: {n_recommended})")
    print(f"  manual overrides:       score={n_score_override} "
          f"recommended={n_rec_override}")
    print(f"  email_drafts (total):   {n_drafts}")
    print(f"  outcomes recorded:      {n_outcomes}")
    print(f"  pending axis suggestions:{n_pending_suggestions}")

    print()
    print("== Last run per stage ==")
    expected = [
        "01_aggregate_sources", "02_enrich_funds", "03_mine_activity",
        "04_mine_partner_signals", "05_verify_and_quality",
        "06_score_candidates", "07_generate_emails", "08_sync_to_attio",
        "attio_outcome_sync", "monthly_learning_report",
    ]
    for st in expected:
        r = last_by_stage.get(st)
        if r is None:
            print(f"  {st:30s} never")
            continue
        # Yellow flag: a stage that ran but processed nothing usable is
        # the "empty pipeline but green vibes" trap. Surface it.
        empty_ingest = (
            (r.records_processed or 0) > 0
            and (r.records_succeeded or 0) == 0
        )
        flag = "  EMPTY" if empty_ingest else ""
        print(
            f"  {st:30s} {_fmt_ts(r.completed_at)}  "
            f"processed={r.records_processed or 0} "
            f"ok={r.records_succeeded or 0} "
            f"failed={r.records_failed or 0} "
            f"skipped={r.records_skipped or 0}"
            f"{flag}"
        )
        if r.error_summary:
            print(f"    -> {r.error_summary[:100]}")

    print()
    print("== CSV review queue ==")
    if csv_path.exists():
        n_rows = sum(1 for _ in csv_path.open(encoding="utf-8")) - 1  # minus header
        print(f"  {csv_path} ({n_rows} row(s))")
    else:
        print(f"  not yet written ({csv_path})")

    if recent_errors:
        print()
        print("== Recent errors (last 5) ==")
        for e in recent_errors:
            msg = (e.error_message or "")[:80]
            print(f"  {_fmt_ts(e.occurred_at)} {e.record_id:40s} "
                  f"{e.error_type}: {msg}")

    # Recommended next command.
    print()
    # Finding 10: don't suggest moving forward if a recent run failed.
    # The operator needs to triage that first.
    failed_stage = None
    for st in expected:
        r = last_by_stage.get(st)
        if r and (r.records_failed or 0) > 0:
            failed_stage = st
            # don't break -- last in `expected` is the most-recent-in-pipeline
    next_cmd = _suggest_next(
        n_funds=n_funds, n_partners=n_partners, n_signals=n_signals,
        n_verified=n_verified, n_summaries=n_summaries,
        n_drafts=n_drafts, csv_exists=csv_path.exists(),
        n_pending_suggestions=n_pending_suggestions,
        failed_stage=failed_stage,
    )
    print(f"== Suggested next ==\n  {next_cmd}")
    return 0


def _suggest_next(*, n_funds, n_partners, n_signals, n_verified,
                  n_summaries, n_drafts, csv_exists,
                  n_pending_suggestions, failed_stage=None) -> str:
    # Finding 10: surface failure first; refuse to "suggest next stage" when
    # the most recent run of a stage failed.
    if failed_stage:
        return (
            f"FIX FIRST: stage {failed_stage} has records_failed > 0 in its "
            f"latest run. Read the error_summary above + recent_errors before "
            f"continuing."
        )
    if n_funds == 0:
        return "uv run scripts/01_aggregate_sources.py"
    if n_partners == 0:
        return "uv run scripts/02_enrich_funds.py --fixtures   # or live"
    if n_signals == 0:
        return ("uv run scripts/03_mine_activity.py --fixtures && "
                "uv run scripts/04_mine_partner_signals.py --fixtures")
    if n_verified == 0:
        return "uv run scripts/05_verify_and_quality.py"
    if n_summaries == 0:
        return "uv run scripts/06_score_candidates.py"
    if n_drafts == 0 or not csv_exists:
        return "uv run scripts/07_generate_emails.py --top 25"
    if n_pending_suggestions > 0:
        return ("review pending suggestions: "
                "uv run python jobs/apply_axis_suggestion.py --list")
    return ("review review_queue.csv; record outcomes with "
            "scripts/record_outcome.py; re-run scripts/07_generate_emails.py "
            "when ready for the next batch")


if __name__ == "__main__":
    raise SystemExit(main())
