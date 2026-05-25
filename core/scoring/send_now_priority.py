"""send_now_priority formula + signal-recency bonus (Refactor item 7/13).

Stage 6 composes a partner's outreach priority from:
  - round_fit_score          (weight 2.0)
  - lead_likelihood_score    (weight 1.5)
  - composite_fit_score      (weight 1.0; treated as 0 when unknown)
  - cold_reachability_score  (weight 0.5)
  - signal_recency_bonus     (+2 / +1 / +0 by quote age band)
  - spiky_belief_score       (additive)
  - major_kill penalty       (-10 when present)

This module owns the formula. Stage 6 stays an orchestrator that
gathers inputs and persists the output.
"""
from __future__ import annotations

from datetime import date


# Signal-recency bands: a more recent quote bumps send_now_priority so
# the operator surfaces hot partners first. Future-dated quotes (bad
# parsing) and missing dates contribute 0 -- days_since() guarantees that.
SIGNAL_RECENCY_90_BONUS_DAYS = 90
SIGNAL_RECENCY_180_BONUS_DAYS = 180

# Major-kill penalty: a partner with a confirmed kill signal lands far
# below baseline even if every other score is high. The value is the
# whole-formula offset, not a multiplier, so a 10-point swing reliably
# pushes a kill-flagged partner outside the top-N pull.
MAJOR_KILL_PENALTY = 10.0

# Weights -- documented here so any future operator-tuning lands in one
# place rather than as magic literals scattered through the formula.
W_ROUND_FIT = 2.0
W_LEAD_LIKELIHOOD = 1.5
W_COMPOSITE = 1.0
W_COLD_REACHABILITY = 0.5


def signal_recency_bonus(most_recent: date | None, today: date) -> float:
    """Recency bonus from the partner's most recent verified quote
    date. days_since() returns None for missing OR future dates, so
    neither inflates the bonus."""
    from core.dates import days_since
    days = days_since(most_recent, today)
    if days is None:
        return 0.0
    if days <= SIGNAL_RECENCY_90_BONUS_DAYS:
        return 2.0
    if days <= SIGNAL_RECENCY_180_BONUS_DAYS:
        return 1.0
    return 0.0


def compute_send_now_priority(
    *,
    round_fit_score: float,
    lead_likelihood_score: float,
    composite_fit_score: float | None,
    cold_reachability_score: float,
    spiky_belief_score: float,
    recency_bonus: float,
    major_kill: bool,
) -> float:
    """Combine per-axis scores into the single send_now_priority
    operators sort by. composite_fit_score may be None when a partner
    has no qualifying signals; it's treated as 0 rather than excluded
    from the sum so the weighting stays comparable across partners.
    """
    comp = composite_fit_score if composite_fit_score is not None else 0.0
    return (
        round_fit_score * W_ROUND_FIT
        + lead_likelihood_score * W_LEAD_LIKELIHOOD
        + comp * W_COMPOSITE
        + cold_reachability_score * W_COLD_REACHABILITY
        + recency_bonus
        + spiky_belief_score
        - (MAJOR_KILL_PENALTY if major_kill else 0.0)
    )
