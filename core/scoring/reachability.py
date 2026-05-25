"""Cold-reachability score derivation (Refactor item 7 / 13).

Stage 6 combines a Stage-4 LLM-derived `cold_reachability_partial_score`
(weight 0.6 -> max contribution 6.0) with two deterministic components
computed from the partner's verified signal dates:

  - post_count_12mo : number of signals within the last 365 days.
    Maps to 0 / 1.0 / 2.0 in three bands.
  - recency_pts     : days since most recent signal.
    Maps to 0 / 1.0 / 2.0 in three bands.

The sum is clamped to [0, 10]. Returns None when no Stage-4 partial is
available (Stage 6's recommendation gate then refuses the partner with
an explicit "reachability unknown" reason rather than treating it as
a zero or a 5).

This module is pure: it consumes a partial score + a list of signal
dicts + today's date. No DB, no LLM.
"""
from __future__ import annotations

from datetime import date


# Stage 4 partial weight + max contribution. The LLM-derived partial is
# 0-10; multiplying by PARTIAL_WEIGHT bounds its contribution to 6.0
# so the deterministic components can fill the remaining 4.0.
PARTIAL_WEIGHT = 0.6

# Bands for post_count_12mo -> posts_pts.
POSTS_HIGH_THRESHOLD = 3
POSTS_LOW_THRESHOLD = 1
POSTS_HIGH_PTS = 2.0
POSTS_LOW_PTS = 1.0

# Bands for "days since most recent signal" -> recency_pts.
RECENCY_HIGH_DAYS = 90
RECENCY_LOW_DAYS = 180
RECENCY_HIGH_PTS = 2.0
RECENCY_LOW_PTS = 1.0

# Final clamp.
REACH_MIN = 0.0
REACH_MAX = 10.0

# Window for post_count_12mo (rolling 12 months).
POST_COUNT_WINDOW_DAYS = 365


def _posts_pts(post_count_12mo: int) -> float:
    if post_count_12mo >= POSTS_HIGH_THRESHOLD:
        return POSTS_HIGH_PTS
    if post_count_12mo >= POSTS_LOW_THRESHOLD:
        return POSTS_LOW_PTS
    return 0.0


def _recency_pts(days_since_most_recent: int | None) -> float:
    if days_since_most_recent is None:
        return 0.0
    if days_since_most_recent <= RECENCY_HIGH_DAYS:
        return RECENCY_HIGH_PTS
    if days_since_most_recent <= RECENCY_LOW_DAYS:
        return RECENCY_LOW_PTS
    return 0.0


def compute_cold_reachability(
    *,
    partial_score: float | None,
    partner_signals: list[dict],
    today: date,
) -> float | None:
    """Combine Stage-4 partial + post count + recency bands into a
    single cold_reachability_score in [0, 10].

    Returns None when partial_score is None (no Stage-4 partial yet).
    Stage 6's recommendation gate treats that as an explicit
    "reachability unknown" failure -- the partner is refused with a
    clear reason rather than scored at a misleading midpoint.

    `partner_signals` is a list of dicts with at least a "date" key
    (datetime.date or None). Future-dated and missing dates are
    excluded from both the post count and the recency band -- see
    core.dates.within_days / days_since.
    """
    if partial_score is None:
        return None
    from core.dates import days_since, within_days
    post_count_12mo = sum(
        1 for s in partner_signals
        if within_days(s.get("date"), POST_COUNT_WINDOW_DAYS, today)
    )
    most_recent = max(
        (s["date"] for s in partner_signals if s.get("date")),
        default=None,
    )
    mr_days = days_since(most_recent, today)
    posts = _posts_pts(post_count_12mo)
    recency = _recency_pts(mr_days)
    total = float(partial_score) * PARTIAL_WEIGHT + posts + recency
    return max(REACH_MIN, min(REACH_MAX, total))
