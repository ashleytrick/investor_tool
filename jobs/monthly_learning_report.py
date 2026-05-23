"""Monthly learning report.

Aggregates the workspace's outcomes against per-axis scores, computes mean
axis score in the booked vs not-booked groups, and writes axis_weight
SUGGESTIONS to the `axis_weight_suggestions` table. Also reports calibration
patterns the spec deliberately did not bake in as weights:

  - reply rate by email strategy
  - reply rate by cold_reachability bucket
  - reply rate by axis_score_variance (spikiness) bucket

**Never modifies axes.yaml directly.** Operator applies suggestions via
jobs/apply_axis_suggestion.py after review.

Confidence per the brief:
  - low if combined sample size under 30
  - medium 30-100
  - high above 100

Cross-workspace learning is OFF by default. If the workspace opts in (via
clients/{workspace}/config/learning.yaml: `cross_workspace_opt_in: true`,
or via attio.yaml `learning.cross_workspace_opt_in`), an ANONYMIZED aggregate
is appended to core/cross_workspace_stats.json. Workspace identity is
sha256-truncated; no partner names, fund names, email text, or signal text
crosses the boundary.

Run: uv run python jobs/monthly_learning_report.py --workspace clients/{name}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml
from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    axis_weight_suggestions,
    email_drafts,
    get_engine,
    outcomes,
    partner_score_summaries,
    scores,
)
from core.runs import RunLogger

STAGE = "monthly_learning_report"
WEIGHT_DELTA_THRESHOLD = 0.5  # axis-score mean diff needed to suggest a change
WEIGHT_STEP = 0.2             # how much to nudge weight per suggestion
MIN_SAMPLE_FOR_SUGGESTION = 2 # minimum total (booked + not_booked) per axis
WEIGHT_CLAMP = (0.1, 2.0)

CROSS_WORKSPACE_STATS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "core" / "cross_workspace_stats.json"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _confidence(n: int) -> str:
    if n < 30:
        return "low"
    if n < 100:
        return "medium"
    return "high"


def _bucket_reach(r: float | None) -> str:
    if r is None:
        return "unknown"
    if r < 4:
        return "low"
    if r < 7:
        return "mid"
    return "high"


def _bucket_variance(v: float | None) -> str:
    if v is None:
        return "unknown"
    if v < 0.5:
        return "flat"
    if v < 1.5:
        return "moderate"
    return "spiky"


def _replied(outcome) -> bool:
    """True if the partner gave any meaningful reply (not no_response/None)."""
    return bool(outcome.reply_type) and outcome.reply_type != "no_response"


def _is_opted_in(ws) -> bool:
    """Cross-workspace learning is opt-in via learning.yaml or attio.yaml."""
    learning_path = ws.config_dir / "learning.yaml"
    if learning_path.exists():
        cfg = yaml.safe_load(learning_path.read_text(encoding="utf-8")) or {}
        if bool(cfg.get("cross_workspace_opt_in")):
            return True
    attio = ws.attio or {}
    attio_inner = attio.get("attio") or attio
    return bool((attio_inner.get("learning") or {}).get("cross_workspace_opt_in"))


def _seed_outcomes(ws, engine) -> int:
    """Replace outcomes rows for each fixture partner. Returns rows written."""
    path = ws.fixtures_dir / "outcomes_seed.json"
    if not path.exists():
        print(f"[learning] --seed-fixture-outcomes: {path} not found; skipping seed")
        return 0
    seed = json.loads(path.read_text(encoding="utf-8"))
    written = 0
    with engine.begin() as conn:
        for row in seed:
            conn.execute(
                delete(outcomes).where(outcomes.c.partner_id == row["partner_id"])
            )
            conn.execute(outcomes.insert().values(
                partner_id=row["partner_id"],
                outreach_status=row.get("outreach_status"),
                reply_type=row.get("reply_type"),
                meeting_booked=bool(row.get("meeting_booked")),
                meeting_outcome=row.get("meeting_outcome"),
                synced_from_attio_at=_now(),
            ))
            written += 1
    print(f"[learning] seeded {written} fixture outcome(s)")
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly learning report.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--seed-fixture-outcomes", action="store_true",
        help="Seed outcomes from data/fixtures/outcomes_seed.json first "
             "(testing only; production runs consume real Attio outcomes).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)

    if args.seed_fixture_outcomes:
        _seed_outcomes(ws, engine)

    with RunLogger(engine, ws.name, STAGE) as run:
        # ---- load per-partner state ----
        with engine.begin() as conn:
            latest_outcomes: dict[str, object] = {}
            for o in conn.execute(select(outcomes).order_by(outcomes.c.outcome_id)):
                latest_outcomes[o.partner_id] = o
            axis_scores: dict[str, dict[str, float]] = defaultdict(dict)
            for s in conn.execute(select(scores)):
                axis_scores[s.partner_id][s.axis_id] = s.score
            summaries: dict[str, object] = {
                r.partner_id: r
                for r in conn.execute(select(partner_score_summaries))
            }
            strategies: dict[str, object] = {}
            for d in conn.execute(
                select(email_drafts).where(email_drafts.c.is_recommended.is_(True))
            ):
                # keep the latest recommended draft per partner
                prev = strategies.get(d.partner_id)
                if prev is None or d.draft_id > prev.draft_id:
                    strategies[d.partner_id] = d

        total = len(latest_outcomes)
        run.processed = total
        if total == 0:
            print(
                "[learning] no outcomes data; report empty. "
                "Use --seed-fixture-outcomes to seed test data, or run "
                "attio_outcome_sync first."
            )
            run.note("no outcomes data")
            return 0

        # ---- by axis: mean booked vs not-booked -> suggestions ----
        axes_cfg = (ws.axes or {}).get("axes") or []
        weights = {a["id"]: float(a.get("weight", 1.0)) for a in axes_cfg}
        suggestions_written = 0
        with engine.begin() as conn:
            for ax in axes_cfg:
                ax_id = ax["id"]
                booked_scores: list[float] = []
                not_booked_scores: list[float] = []
                for pid, outcome in latest_outcomes.items():
                    s = axis_scores.get(pid, {}).get(ax_id)
                    if s is None:
                        continue
                    (booked_scores if outcome.meeting_booked
                     else not_booked_scores).append(s)
                n_total = len(booked_scores) + len(not_booked_scores)
                if n_total < MIN_SAMPLE_FOR_SUGGESTION:
                    continue
                if not booked_scores or not_booked_scores == []:
                    # Need at least one on each side to compute diff. Without
                    # both sides we can't tell which way to nudge.
                    if not booked_scores or not not_booked_scores:
                        continue
                mean_b = mean(booked_scores)
                mean_n = mean(not_booked_scores)
                diff = mean_b - mean_n
                if abs(diff) < WEIGHT_DELTA_THRESHOLD:
                    continue
                current_w = weights.get(ax_id, 1.0)
                suggested = max(
                    WEIGHT_CLAMP[0],
                    min(
                        WEIGHT_CLAMP[1],
                        current_w + (WEIGHT_STEP if diff > 0 else -WEIGHT_STEP),
                    ),
                )
                reason = (
                    f"axis {ax_id}: booked mean {mean_b:.2f} vs "
                    f"not-booked {mean_n:.2f} (diff {diff:+.2f}, n={n_total})"
                )
                conn.execute(axis_weight_suggestions.insert().values(
                    generated_at=_now(),
                    axis_id=ax_id,
                    current_weight=current_w,
                    suggested_weight=suggested,
                    reason=reason,
                    confidence=_confidence(n_total),
                    sample_size=n_total,
                ))
                suggestions_written += 1
                print(
                    f"[learning] suggestion: {reason} -> "
                    f"current={current_w:.2f} suggested={suggested:.2f} "
                    f"confidence={_confidence(n_total)}"
                )

        # ---- calibration: reply rate buckets (printed, not auto-applied) ----
        booked = sum(1 for o in latest_outcomes.values() if o.meeting_booked)
        replied = sum(1 for o in latest_outcomes.values() if _replied(o))
        print(
            f"[learning] outcomes summary: total={total} replied={replied} "
            f"booked={booked}"
        )

        def _bucket_report(label: str, table: dict[str, list[int]]) -> dict[str, float]:
            rates: dict[str, float] = {}
            for key, (r, t) in table.items():
                rate = r / t if t else 0.0
                rates[key] = rate
                print(f"[learning] {label}={key}: reply_rate={rate:.0%} ({r}/{t})")
            return rates

        strat_table: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for pid, outcome in latest_outcomes.items():
            draft = strategies.get(pid)
            if not draft:
                continue
            strat_table[draft.strategy][1] += 1
            if _replied(outcome):
                strat_table[draft.strategy][0] += 1
        strat_rates = _bucket_report("strategy", strat_table)

        reach_table: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for pid, outcome in latest_outcomes.items():
            summary = summaries.get(pid)
            if not summary:
                continue
            reach_table[_bucket_reach(summary.cold_reachability_score)][1] += 1
            if _replied(outcome):
                reach_table[_bucket_reach(summary.cold_reachability_score)][0] += 1
        reach_rates = _bucket_report("reachability", reach_table)

        var_table: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for pid, outcome in latest_outcomes.items():
            summary = summaries.get(pid)
            if not summary:
                continue
            var_table[_bucket_variance(summary.axis_score_variance)][1] += 1
            if _replied(outcome):
                var_table[_bucket_variance(summary.axis_score_variance)][0] += 1
        var_rates = _bucket_report("variance", var_table)

        # ---- cross-workspace stats (opt-in only) ----
        if _is_opted_in(ws):
            existing: dict = {}
            if CROSS_WORKSPACE_STATS_PATH.exists():
                try:
                    existing = json.loads(
                        CROSS_WORKSPACE_STATS_PATH.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    existing = {}
            wid = hashlib.sha256(ws.name.encode("utf-8")).hexdigest()[:12]
            existing.setdefault("contributors", [])
            if wid not in existing["contributors"]:
                existing["contributors"].append(wid)
            existing.setdefault("workspace_stats", {})[wid] = {
                "strategy_to_reply_rate": strat_rates,
                "reachability_bucket_to_reply_rate": reach_rates,
                "variance_bucket_to_reply_rate": var_rates,
                "sample_size": total,
                "updated_at": _now().isoformat(),
            }
            existing["last_updated"] = _now().isoformat()
            CROSS_WORKSPACE_STATS_PATH.write_text(
                json.dumps(existing, indent=2), encoding="utf-8"
            )
            print(
                f"[learning] cross-workspace stats updated "
                f"(workspace_hash={wid}, anonymized aggregates only)"
            )

        run.succeeded = suggestions_written
        run.note(f"suggestions_written={suggestions_written}")
        print(f"[learning] {suggestions_written} suggestion(s) written")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
