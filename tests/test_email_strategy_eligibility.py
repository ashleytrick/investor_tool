"""Unit tests for core/email/strategy_eligibility.py (Refactor 7/14).

Pure-function tests for the Stage 7 strategy picker. No subprocess,
no workspace fixture, no DB.
"""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.strategy_eligibility import (
    MARKET_SHIFT_AXIS_TOKENS,
    METRICS_SIGNAL_KEYWORDS,
    STRATEGY_TIE_BREAK,
    compute_eligibility,
    has_company_traction,
    has_metrics_oriented_signal,
    market_shift_axis_ids,
    pick_strategies,
)


# ----- compute_eligibility -----


def test_signal_led_promotes_with_q3_quote() -> None:
    elig = compute_eligibility(
        has_q3=True, has_q2=True, fund_adjacent=False,
        partner_led_in_target=False, market_window_match=False,
        company_traction_proof=False,
    )
    assert elig["signal_led"] == 3
    assert elig["contrarian_thesis_led"] == 2  # also requires q3


def test_signal_led_falls_to_two_with_only_q2() -> None:
    elig = compute_eligibility(
        has_q3=False, has_q2=True, fund_adjacent=False,
        partner_led_in_target=False, market_window_match=False,
        company_traction_proof=False,
    )
    assert elig["signal_led"] == 2
    assert elig["contrarian_thesis_led"] == 0  # contrarian needs q3 specifically


def test_signal_led_zero_with_no_evidence() -> None:
    elig = compute_eligibility(
        has_q3=False, has_q2=False, fund_adjacent=False,
        partner_led_in_target=False, market_window_match=False,
        company_traction_proof=False,
    )
    assert elig == {
        "signal_led": 0,
        "portfolio_led": 0,
        "round_pattern_led": 0,
        "market_shift_led": 0,
        "contrarian_thesis_led": 0,
        "traction_led": 0,
    }


def test_market_shift_caps_at_two_even_with_match() -> None:
    elig = compute_eligibility(
        has_q3=False, has_q2=False, fund_adjacent=False,
        partner_led_in_target=False, market_window_match=True,
        company_traction_proof=False,
    )
    assert elig["market_shift_led"] == 2


def test_traction_led_caps_at_two() -> None:
    elig = compute_eligibility(
        has_q3=False, has_q2=False, fund_adjacent=False,
        partner_led_in_target=False, market_window_match=False,
        company_traction_proof=True,
    )
    assert elig["traction_led"] == 2


# ----- pick_strategies -----


def test_pick_strategies_returns_at_most_two() -> None:
    elig = {
        "signal_led": 3, "portfolio_led": 3, "round_pattern_led": 3,
        "market_shift_led": 2, "traction_led": 2, "contrarian_thesis_led": 2,
    }
    picks = pick_strategies(elig)
    assert len(picks) == 2


def test_pick_strategies_filters_below_two() -> None:
    elig = {
        "signal_led": 2, "portfolio_led": 1, "round_pattern_led": 0,
        "market_shift_led": 0, "traction_led": 0, "contrarian_thesis_led": 0,
    }
    assert pick_strategies(elig) == ["signal_led"]


def test_pick_strategies_tiebreaks_in_canonical_order() -> None:
    elig = {
        "signal_led": 2, "portfolio_led": 2, "round_pattern_led": 2,
        "market_shift_led": 2, "traction_led": 2, "contrarian_thesis_led": 2,
    }
    # All tied at 2; tie-break gives signal_led, portfolio_led.
    assert pick_strategies(elig) == ["signal_led", "portfolio_led"]


def test_pick_strategies_higher_score_wins_over_tie_break() -> None:
    # contrarian is last in tie-break order but scores higher than
    # signal_led, so it should appear first.
    elig = {
        "signal_led": 2, "contrarian_thesis_led": 3, "portfolio_led": 0,
        "round_pattern_led": 0, "market_shift_led": 0, "traction_led": 0,
    }
    assert pick_strategies(elig) == ["contrarian_thesis_led", "signal_led"]


def test_pick_strategies_empty_when_nothing_eligible() -> None:
    elig = {k: 0 for k in STRATEGY_TIE_BREAK}
    assert pick_strategies(elig) == []


# ----- has_metrics_oriented_signal -----


def test_metrics_signal_detected_on_arr_keyword() -> None:
    signals = [{"quote": "we hit $1M ARR in 6 months"}]
    assert has_metrics_oriented_signal(signals) is True


def test_metrics_signal_detects_design_partner_phrase() -> None:
    signals = [{"quote": "looking for design partners in fintech"}]
    assert has_metrics_oriented_signal(signals) is True


def test_metrics_signal_missing_in_non_metric_quote() -> None:
    signals = [{"quote": "AI is changing how we think about UX"}]
    assert has_metrics_oriented_signal(signals) is False


def test_metrics_signal_empty_partner_signal_list() -> None:
    assert has_metrics_oriented_signal([]) is False


def test_metrics_signal_handles_missing_quote_key() -> None:
    """Defensive: signals from non-extraction paths may lack 'quote'."""
    assert has_metrics_oriented_signal([{}, {"quote": None}]) is False


# ----- market_shift_axis_ids -----


def test_market_shift_axis_matched_via_name() -> None:
    cfg = {"axes": [
        {"id": "axis_timing", "name": "Timing"},
        {"id": "axis_market", "name": "Market shift"},
        {"id": "axis_other", "name": "Founder profile"},
    ]}
    assert market_shift_axis_ids(cfg) == {"axis_timing", "axis_market"}


def test_market_shift_axis_matched_via_description() -> None:
    cfg = {"axes": [
        {"id": "axis_a", "name": "Regulation",
         "description": "Forced buy from policy windows"},
    ]}
    assert market_shift_axis_ids(cfg) == {"axis_a"}


def test_market_shift_axis_empty_when_no_match() -> None:
    cfg = {"axes": [{"id": "axis_a", "name": "Customer evidence"}]}
    assert market_shift_axis_ids(cfg) == set()


def test_market_shift_axis_tolerates_missing_axes_block() -> None:
    assert market_shift_axis_ids({}) == set()
    assert market_shift_axis_ids({"axes": None}) == set()


def test_market_shift_axis_drops_blank_ids() -> None:
    """An axis row without an id is malformed config -- ignore it
    silently rather than returning {None}."""
    cfg = {"axes": [{"name": "Timing", "id": ""}]}
    assert market_shift_axis_ids(cfg) == set()


# ----- has_company_traction -----


def test_has_traction_via_headline_metric() -> None:
    cfg = {"company": {"current_traction": {"headline_metric": "$2M ARR"}}}
    assert has_company_traction(cfg) is True


def test_has_traction_via_secondary_metrics() -> None:
    cfg = {"company": {"current_traction": {"secondary_metrics": ["NRR 128%"]}}}
    assert has_company_traction(cfg) is True


def test_no_traction_without_metric_fields() -> None:
    cfg = {"company": {"current_traction": {}}}
    assert has_company_traction(cfg) is False


def test_no_traction_when_current_traction_missing() -> None:
    cfg = {"company": {}}
    assert has_company_traction(cfg) is False


def test_strategy_tie_break_covers_all_six_strategies() -> None:
    """Guard: STRATEGY_TIE_BREAK must list every strategy
    compute_eligibility returns so pick_strategies' sort key never
    raises ValueError on .index()."""
    elig = compute_eligibility(False, False, False, False, False, False)
    assert set(STRATEGY_TIE_BREAK) == set(elig.keys())
