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

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import get_engine, signals
from core.llm.client import LLMClient
from core.runs import RunLogger
from core.signal_quality import score_signal
from core.verification import verify_signal

STAGE = "05_verify_and_quality"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 5 verify + quality.")
    add_workspace_arg(parser)
    parser.add_argument("--force", action="store_true",
                        help="Re-verify and re-score every signal, not just unprocessed.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)
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

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        verified_count = 0
        quality2_plus = 0
        method_counts: dict[str, int] = {}

        for s in rows:
            run.processed += 1
            try:
                ver = verify_signal(
                    engine, s.source_url, s.quoted_text, s.snapshot_id
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
                    with engine.begin() as conn:
                        conn.execute(
                            signals.update().where(signals.c.signal_id == s.signal_id)
                            .values(
                                verified=False,
                                verification_method=ver.verification_method,
                                verification_error=ver.verification_error,
                            )
                        )
                run.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - logged, continue
                run.failed += 1
                run.log_error(str(s.signal_id), type(exc).__name__, str(exc))

        total = max(run.processed, 1)
        pct = verified_count * 100.0 / total
        print(
            f"[stage 5] verified {verified_count}/{run.processed} "
            f"({pct:.0f}%) | quality>=2: {quality2_plus} | "
            f"methods: {method_counts}"
        )
        if 50 <= pct <= 80:
            print("[stage 5] verification rate within 50-80% expected band for real data")
        else:
            print(
                "[stage 5] verification rate outside 50-80% band. "
                "Expected for fixture runs (hand-authored snapshots all match); "
                "recalibrate Stage 4 prompts before scaling real data."
            )
        print(f"[stage 5] llm stub mode: {llm.stub}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
