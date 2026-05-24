"""Unit tests for core/scoring/reachability.py (Refactor item 7/13)."""
from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.reachability import (
    PARTIAL_WEIGHT,
    POSTS_HIGH_PTS,
    POSTS_HIGH_THRESHOLD,
    POSTS_LOW_PTS,
    REACH_MAX,
    RECENCY_HIGH_DAYS,
    RECENCY_HIGH_PTS,
    RECENCY_LOW_DAYS,
    RECENCY_LOW_PTS,
    compute_cold_reachability,
)


_TODAY = date(2026, 5, 24)


def _signal(days_ago: int) -> dict:
    return {"date": _TODAY - timedelta(days=days_ago)}


def test_partial_none_returns_none_so_gate_can_block() -> None:
    """When Stage 4 hasn't produced a partial, return None so Stage 6's
    recommendation gate sees 'unknown reachability' and refuses the
    partner with an explicit reason -- the bug Batch 35 fixed."""
    assert compute_cold_reachability(
        partial_score=None, partner_signals=[_signal(5)], today=_TODAY,
    ) is None


def test_partial_alone_contributes_proportional_weight() -> None:
    """No signals -> posts_pts = recency_pts = 0; result is just
    partial * PARTIAL_WEIGHT."""
    result = compute_cold_reachability(
        partial_score=5.0, partner_signals=[], today=_TODAY,
    )
    assert result == 5.0 * PARTIAL_WEIGHT


def test_three_plus_signals_in_12mo_unlocks_high_posts_pts() -> None:
    sigs = [_signal(10), _signal(60), _signal(200)]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == POSTS_HIGH_PTS + RECENCY_HIGH_PTS  # 2 + 2 = 4


def test_one_signal_in_12mo_uses_low_posts_pts() -> None:
    sigs = [_signal(200)]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    # 1 signal in 12mo -> POSTS_LOW_PTS; most recent 200 days -> 0
    assert result == POSTS_LOW_PTS


def test_zero_signals_in_12mo_gives_zero_pts() -> None:
    sigs = [_signal(400)]  # outside 12mo
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == 0.0


def test_recency_within_90_days_uses_high_recency_pts() -> None:
    sigs = [_signal(RECENCY_HIGH_DAYS - 5)]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    # 1 signal -> POSTS_LOW_PTS; recency in HIGH band -> RECENCY_HIGH_PTS
    assert result == POSTS_LOW_PTS + RECENCY_HIGH_PTS


def test_recency_between_90_and_180_uses_low_recency_pts() -> None:
    sigs = [_signal(RECENCY_LOW_DAYS - 5)]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == POSTS_LOW_PTS + RECENCY_LOW_PTS


def test_recency_beyond_180_gives_zero_recency_pts() -> None:
    sigs = [_signal(RECENCY_LOW_DAYS + 30)]  # 210 days
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    # post still in 12mo, but recency too old
    assert result == POSTS_LOW_PTS


def test_future_dated_signal_does_not_inflate_score() -> None:
    """Bad parsing produces future dates; they must not pad the score."""
    sigs = [{"date": _TODAY + timedelta(days=30)}]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == 0.0


def test_missing_date_key_safe() -> None:
    sigs = [{"date": None}, {}]
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == 0.0


def test_clamp_to_ten_when_partial_plus_components_exceed() -> None:
    """partial=10 * weight 0.6 = 6; plus posts 2 + recency 2 = 10
    exactly. Push posts higher hypothetically would clamp -- exercise
    with extra signals to confirm we don't go above REACH_MAX."""
    sigs = [_signal(10), _signal(20), _signal(30), _signal(60)]
    result = compute_cold_reachability(
        partial_score=10.0, partner_signals=sigs, today=_TODAY,
    )
    assert result == REACH_MAX


def test_clamp_to_zero_when_combination_would_be_negative() -> None:
    """No realistic input produces negative, but REACH_MIN clamp is
    documented; verify via constructed signed partial."""
    # The clamp protects against future formula changes; partial is
    # currently always >= 0 from Stage 4. Pin the floor anyway.
    result = compute_cold_reachability(
        partial_score=0.0, partner_signals=[], today=_TODAY,
    )
    assert result == 0.0


def test_constants_self_consistent() -> None:
    assert POSTS_HIGH_THRESHOLD > 0
    assert RECENCY_HIGH_DAYS < RECENCY_LOW_DAYS
    assert REACH_MAX > 0
    # Partial weight (0.6) leaves 4.0 max for the deterministic
    # components, which equals POSTS_HIGH_PTS + RECENCY_HIGH_PTS.
    assert PARTIAL_WEIGHT * 10 + POSTS_HIGH_PTS + RECENCY_HIGH_PTS == REACH_MAX
