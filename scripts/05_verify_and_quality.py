"""Stage 5: verification gauntlet + signal quality scoring.

For each signal that hasn't been fully processed, runs the live-then-snapshot
verification gauntlet (core.verification) and, when verified, scores quality
0-3 (core.signal_quality) against the shared calibration set. Downstream:
  - signal_quality_score >= 2 may support Stage 6 scoring
  - signal_quality_score >= 3 may open a signal_led email in Stage 7

Run: uv run scripts/05_verify_and_quality.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.config_loader import add_workspace_arg
from core.db import signals
from core.stage_runner import stage_run
from core.signal_quality import score_signal
from core.verification import verify_signal

STAGE = "05_verify_and_quality"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 5 verify + quality.")
    add_workspace_arg(parser)
    parser.add_argument("--force", action="store_true",
                        help="Re-verify and re-score every signal, not just "
                             "unprocessed. With --offline this is fast because "
                             "no live fetches happen (Batch 28 #353).")
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip live fetch; verify only against captured snapshots "
             "(Batch 28 #354). Use when the network is unreliable or you "
             "want to validate that snapshots alone still match. Marks "
             "snapshot-only verifications with method='snapshot_fallback' "
             "and unresolved cases with method='no_snapshot_offline' or "
             "'quote_not_in_snapshot' so the audit reads cleanly.",
    )
    parser.add_argument(
        "--allow-verification-rate-outside-band", action="store_true",
        help="Bypass the 50-80% live-mode verification-rate gate. Requires "
             "--reason. Use after you've reviewed why verification is "
             "drifting (LLM hallucinated quotes, URL rot, snapshot misses).",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Required with --allow-verification-rate-outside-band.",
    )
    args = parser.parse_args()
    if args.allow_verification_rate_outside_band and not args.reason:
        parser.error(
            "--allow-verification-rate-outside-band requires --reason \"...\""
        )

    # Refactor sweep: stage_run() boilerplate collapse.
    with stage_run(args, stage=STAGE) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm
        company_name = ws.company["company"]["name"]
        company_desc = ws.company["company"]["description"]

        with engine.begin() as conn:
            cond = (
                (signals.c.verified.is_(False))
                | (signals.c.signal_quality_score.is_(None))
            )
            if args.force:
                cond = None
            stmt = select(signals)
            if cond is not None:
                stmt = stmt.where(cond)
            rows = list(conn.execute(stmt))

        verified_count = 0
        quality2_plus = 0
        method_counts: dict[str, int] = {}
        verified_count = 0
        quality2_plus = 0
        method_counts: dict[str, int] = {}

        for s in rows:
            with run.attempt():
                try:
                    ver = verify_signal(
                        engine, s.source_url, s.quoted_text, s.snapshot_id,
                        offline=args.offline,
                    )
                    method_counts[ver.verification_method] = (
                        method_counts.get(ver.verification_method, 0) + 1
                    )
                    if ver.verified:
                        verified_count += 1
                        import json as _json
                        axes = _json.loads(s.axis_relevance or "[]")
                        quality = score_signal(
                            llm,
                            quoted_text=s.quoted_text,
                            axis_relevance=axes,
                            quote_date=s.quote_date.isoformat() if s.quote_date else None,
                            source_url=s.source_url,
                            signal_direction=s.signal_direction or "positive",
                            confidence="high",
                            company_description=company_desc,
                            company_name=company_name,
                        )
                        if quality.signal_quality_score >= 2:
                            quality2_plus += 1
                        with engine.begin() as conn:
                            conn.execute(
                                signals.update().where(signals.c.signal_id == s.signal_id)
                                .values(
                                    verified=True,
                                    verification_method=ver.verification_method,
                                    verification_error=None,
                                    signal_quality_score=quality.signal_quality_score,
                                    quality_reasoning=quality.quality_reasoning,
                                )
                            )
                    else:
                        # Batch 11 (#351/#352): when verification flips to False,
                        # the previously-set signal_quality_score and
                        # quality_reasoning become misleading -- they describe a
                        # quote that no longer verifies. Clear them so Stage 6's
                        # quality>=2 filter and Stage 7's signal_led eligibility
                        # don't pick up an unverified signal that still carries
                        # a stale quality score.
                        with engine.begin() as conn:
                            conn.execute(
                                signals.update().where(signals.c.signal_id == s.signal_id)
                                .values(
                                    verified=False,
                                    verification_method=ver.verification_method,
                                    verification_error=ver.verification_error,
                                    signal_quality_score=None,
                                    quality_reasoning=None,
                                )
                            )
                except Exception as exc:  # noqa: BLE001 - logged, continue
                    run.fail(str(s.signal_id), type(exc).__name__, str(exc))

        total = max(run.processed, 1)
        pct = verified_count * 100.0 / total
        print(
            f"[stage 5] verified {verified_count}/{run.processed} "
            f"({pct:.0f}%) | quality>=2: {quality2_plus} | "
            f"methods: {method_counts}"
        )

        # Verification-rate gate is BINDING in live mode. The brief's 50-80%
        # band catches the two real failure modes on real data: LLM
        # hallucinating quotes (rate too low) and snapshot-only matches that
        # never actually validate against the live page (rate too high but
        # via the wrong method).
        in_band = 50 <= pct <= 80
        sample_too_small = run.processed < 10
        if in_band:
            print("[stage 5] verification rate within 50-80% expected band")
        elif llm.stub or sample_too_small or args.offline:
            # Batch 28: offline runs verify only against snapshots, so the
            # rate is dominated by snapshot availability, not LLM
            # hallucination -- the band check doesn't apply.
            print(
                "[stage 5] verification rate outside 50-80% band -- "
                "expected for fixture runs (clean snapshots), small "
                f"samples (n={run.processed}), or --offline mode; not enforced."
            )
        elif args.allow_verification_rate_outside_band:
            note = (
                f"verification rate {pct:.0f}% outside 50-80% band; "
                f"approved bypass: {args.reason!r}"
            )
            print(f"[stage 5] {note}")
            run.note(note)
        else:
            ctx.refuse(
                f"FAIL: verification rate {pct:.0f}% outside 50-80% band "
                f"on live data (n={run.processed}). Recalibrate Stage 4 "
                f"prompts OR pass --allow-verification-rate-outside-band "
                f"--reason ..."
            )
            print(f"[stage 5] REFUSED: see runs.error_summary")
            print(f"[stage 5] llm stub mode: {llm.stub}")
            return ctx.exit_code
        print(f"[stage 5] llm stub mode: {llm.stub}")
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
