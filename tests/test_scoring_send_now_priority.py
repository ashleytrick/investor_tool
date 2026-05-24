"""Unit tests for core/scoring/send_now_priority.py (Refactor 7/13)."""
from __future__ import annotations

from datetime import date, timedelta

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.send_now_priority import (
    MAJOR_KILL_PENALTY,
    SIGNAL_RECENCY_90_BONUS_DAYS,
    SIGNAL_RECENCY_180_BONUS_DAYS,
    compute_send_now_priority,
    signal_recency_bonus,
)


# ----- signal_recency_bonus -----


def test_recency_bonus_two_within_90_days() -> None:
    today = date(2026, 5, 24)
    quote = today - timedelta(days=30)
    assert signal_recency_bonus(quote, today) == 2.0


def test_recency_bonus_one_between_90_and_180_days() -> None:
    today = date(2026, 5, 24)
    quote = today - timedelta(days=120)
    assert signal_recency_bonus(quote, today) == 1.0


def test_recency_bonus_zero_outside_180_days() -> None:
    today = date(2026, 5, 24)
    quote = today - timedelta(days=200)
    assert signal_recency_bonus(quote, today) == 0.0


def test_recency_bonus_zero_for_missing_date() -> None:
    today = date(2026, 5, 24)
    assert signal_recency_bonus(None, today) == 0.0


def test_recency_bonus_zero_for_future_date() -> None:
    """Future-dated quotes (bad parsing) shouldn't inflate the bonus."""
    today = date(2026, 5, 24)
    quote = today + timedelta(days=30)
    assert signal_recency_bonus(quote, today) == 0.0


def test_recency_bonus_constants_match_brief() -> None:
    assert SIGNAL_RECENCY_90_BONUS_DAYS == 90
    assert SIGNAL_RECENCY_180_BONUS_DAYS == 180


# ----- compute_send_now_priority -----


def _base(**over) -> dict:
    """Baseline kwargs: weights produce a clean expected total."""
    args = dict(
        round_fit_score=5.0,            # *2.0 = 10
        lead_likelihood_score=4.0,      # *1.5 = 6
        composite_fit_score=8.0,        # *1.0 = 8
        cold_reachability_score=6.0,    # *0.5 = 3
        spiky_belief_score=1.5,         #         1.5
        recency_bonus=2.0,              #         2.0
        major_kill=False,               #         0
    )
    args.update(over)
    return args


def test_formula_matches_documented_weights() -> None:
    # 10 + 6 + 8 + 3 + 1.5 + 2.0 = 30.5
    assert compute_send_now_priority(**_base()) == 30.5


def test_composite_fit_score_none_is_treated_as_zero() -> None:
    base_total = compute_send_now_priority(**_base())
    no_composite = compute_send_now_priority(**_base(composite_fit_score=None))
    assert no_composite == base_total - 8.0  # composite term was 8


def test_major_kill_subtracts_full_penalty() -> None:
    base_total = compute_send_now_priority(**_base())
    killed = compute_send_now_priority(**_base(major_kill=True))
    assert killed == base_total - MAJOR_KILL_PENALTY


def test_negative_score_possible_when_kill_dominates() -> None:
    """A partner with low signals + major_kill should land NEGATIVE so
    they sort below baseline zero-score partners."""
    score = compute_send_now_priority(
        round_fit_score=0.0,
        lead_likelihood_score=0.0,
        composite_fit_score=None,
        cold_reachability_score=0.0,
        spiky_belief_score=0.0,
        recency_bonus=0.0,
        major_kill=True,
    )
    assert score == -MAJOR_KILL_PENALTY


def test_spiky_belief_and_recency_add_directly() -> None:
    """spiky_belief and recency are additive (no weighting); raising
    either by N raises the total by N."""
    base_total = compute_send_now_priority(**_base())
    plus_spiky = compute_send_now_priority(**_base(spiky_belief_score=3.0))
    plus_recency = compute_send_now_priority(**_base(recency_bonus=3.0))
    assert plus_spiky == base_total + 1.5  # 3.0 - 1.5 baseline
    assert plus_recency == base_total + 1.0  # 3.0 - 2.0 baseline
