"""Composite axis score + spikiness derivation (Refactor item 7/13).

Stage 6's per-partner axis scores (LLM-derived or deterministic stub)
get aggregated into a single composite_fit_score plus a few derived
properties:

  - composite        weighted average across axes that have a score
  - axis_max         the highest single-axis score
  - variance         per-axis variance
  - spiky            clamped 0-2 derived from variance; rewards
                     partners with a strong belief on at least one
                     axis even when their average isn't elite
  - score_confidence "high" / "medium" / "low" based on axis coverage

Plus the deterministic stub axis scorer used in offline / fixture
runs.

This module is pure: it consumes config dicts + a parsed
CandidateScore and returns plain values. No DB, no LLM.
"""
from __future__ import annotations

from typing import Any


# Stub axis scoring constants (kept on this page so an operator
# tuning the offline scorer doesn't have to dig through the function
# body).
STUB_NEUTRAL = 6.0
STUB_MAX = 10.0
STUB_MIN = 0.0
STUB_Q3_STEP = 1.0  # each Q3 quote shifts by 1.0 (capped at 3 quotes)
STUB_Q2_BUMP = 0.5  # any Q2 quotes contribute a single 0.5 bump

# Composite confidence bands (based on number of scored axes).
CONFIDENCE_HIGH_MIN_AXES = 4
CONFIDENCE_MEDIUM_MIN_AXES = 2

# Spikiness derivation: clamp variance * SPIKY_VAR_WEIGHT to [0, SPIKY_MAX].
SPIKY_VAR_WEIGHT = 0.5
SPIKY_MAX = 2.0


def stub_axis_scores(verified_signals: list[dict], axes_cfg: dict) -> dict:
    """Deterministic per-axis stub used when the LLM client is offline.

    Signal direction matters: a 'negative' quote on an axis is
    evidence the partner DOES NOT hold that belief, so it should LOWER
    the axis score, not raise it. The previous version counted all
    signals as positive evidence, so an anti-fit quote tagged to the
    regulated-market axis would bump the score for that axis by
    0.5-1.0.
    """
    by_axis: dict[str, dict] = {}
    for ax in axes_cfg.get("axes", []):
        ax_id = ax["id"]
        relevant = [s for s in verified_signals if ax_id in s["axes"]]
        if not relevant:
            by_axis[ax_id] = {
                "score": None,
                "supporting_signal_ids": [],
                "confidence": "low",
                "reasoning": "no verified quality>=2 signals on this axis",
            }
            continue
        pos = [
            s for s in relevant
            if (s.get("direction") or "").lower() == "positive"
        ]
        neg = [
            s for s in relevant
            if (s.get("direction") or "").lower() == "negative"
        ]
        q3 = sum(1 for s in pos if s["quality"] == 3)
        q2 = sum(1 for s in pos if s["quality"] == 2)
        q3_neg = sum(1 for s in neg if s["quality"] == 3)
        q2_neg = sum(1 for s in neg if s["quality"] == 2)
        # Start at neutral. Positive signals raise; negative signals
        # subtract proportionally. Clamp so a partner with several
        # anti-fit quotes lands at STUB_MIN, not below.
        score = STUB_NEUTRAL + min(3, q3) * STUB_Q3_STEP + (
            STUB_Q2_BUMP if q2 else 0.0
        )
        score -= min(3, q3_neg) * STUB_Q3_STEP + (
            STUB_Q2_BUMP if q2_neg else 0.0
        )
        score = max(STUB_MIN, min(STUB_MAX, score))
        confidence = (
            "high" if len(relevant) >= 2
            else ("medium" if (q3 or q3_neg) else "low")
        )
        by_axis[ax_id] = {
            "score": float(score),
            "supporting_signal_ids": [s["id"] for s in relevant],
            "confidence": confidence,
            "reasoning": (
                f"stub: pos={q3}xQ3+{q2}xQ2, neg={q3_neg}xQ3+{q2_neg}xQ2 "
                f"tagged on this axis"
            ),
        }
    return by_axis


def composite_and_spikiness(
    candidate_score: Any, axes_cfg: dict,
) -> tuple[float | None, float | None, float, float, str]:
    """Aggregate per-axis scores into (composite, axis_max, variance,
    spiky, score_confidence).

    Returns (None, None, 0.0, 0.0, "low") when no axis has a score --
    the caller persists composite=NULL so Stage 6's recommendation gate
    fails the partner on missing composite rather than treating it as 0.

    `candidate_score` is a CandidateScore Pydantic instance whose
    `axis_scores` attribute is a dict of axis_id -> object with .score
    / .supporting_signal_ids / .confidence / .reasoning. Typing
    intentionally loose so this module doesn't depend on the schemas
    package.
    """
    weights_by_id = {
        ax["id"]: float(ax.get("weight", 1.0))
        for ax in axes_cfg["axes"]
    }
    scored = [
        (ax_id, ax_data)
        for ax_id, ax_data in candidate_score.axis_scores.items()
        if ax_data.score is not None
    ]
    if not scored:
        return None, None, 0.0, 0.0, "low"

    total_w = sum(weights_by_id.get(ax_id, 1.0) for ax_id, _ in scored)
    weighted = sum(
        ax_data.score * weights_by_id.get(ax_id, 1.0)
        for ax_id, ax_data in scored
    )
    composite = weighted / total_w
    score_values = [ax_data.score for _, ax_data in scored]
    axis_max = max(score_values)
    if len(score_values) > 1:
        mean = sum(score_values) / len(score_values)
        variance = sum((s - mean) ** 2 for s in score_values) / len(score_values)
    else:
        variance = 0.0
    spiky = max(0.0, min(SPIKY_MAX, variance * SPIKY_VAR_WEIGHT))

    n = len(scored)
    confidence = (
        "high" if n >= CONFIDENCE_HIGH_MIN_AXES
        else ("medium" if n >= CONFIDENCE_MEDIUM_MIN_AXES else "low")
    )
    return composite, axis_max, variance, spiky, confidence
