"""Unit tests for core/scoring/recommendation.py (Refactor item 7 / 13).

These exercise the gate as a pure function -- no subprocess, no DB, no
workspace fixture. They cover the edge cases that previously required
spinning up an entire pipeline run to test.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.recommendation import (
    COLD_REACHABILITY_MIN,
    COMPOSITE_MIN,
    LEAD_LIKELIHOOD_MIN,
    RECENT_EVIDENCE_WINDOW_DAYS,
    ROUND_FIT_MIN,
    evaluate_recommended,
)


def _green(**overrides) -> dict:
    """Baseline 'all criteria met' kwargs. Override single fields to
    test specific failure paths."""
    base = dict(
        composite=7.5,
        round_fit_score=7.0,
        disqualifier_present=False,
        lead_likelihood_score=6.0,
        distinct_source_types=2,
        q2_plus_signal_count=2,
        deal_attribution_count=1,
        most_recent_signal_date=date.today(),
        employment_status="likely_current",
        major_kill=False,
        cold_reachability_score=7.0,
        warm_path_available=False,
        today=date.today(),
    )
    base.update(overrides)
    return base


def test_all_criteria_met_recommends() -> None:
    ok, reason = evaluate_recommended(**_green())
    assert ok is True
    assert "All Stage 6 criteria met" in reason


def test_low_composite_blocks() -> None:
    ok, reason = evaluate_recommended(**_green(composite=6.0))
    assert ok is False
    assert f"< {COMPOSITE_MIN}" in reason


def test_low_round_fit_blocks() -> None:
    ok, reason = evaluate_recommended(**_green(round_fit_score=5.5))
    assert ok is False
    assert "round_fit_score" in reason


def test_disqualifier_blocks_even_with_high_round_fit() -> None:
    ok, _ = evaluate_recommended(**_green(disqualifier_present=True))
    assert ok is False


def test_lead_likelihood_none_blocks_with_explanation() -> None:
    ok, reason = evaluate_recommended(**_green(lead_likelihood_score=None))
    assert ok is False
    assert "lead_likelihood_score is unknown" in reason


def test_lead_likelihood_below_threshold_blocks() -> None:
    ok, reason = evaluate_recommended(
        **_green(lead_likelihood_score=LEAD_LIKELIHOOD_MIN - 1)
    )
    assert ok is False
    assert "lead_likelihood_score" in reason


def test_single_source_type_blocks_when_no_deal_evidence() -> None:
    ok, reason = evaluate_recommended(
        **_green(distinct_source_types=1, deal_attribution_count=0),
    )
    assert ok is False
    assert "2 distinct" in reason


def test_single_source_passes_when_paired_with_deal_evidence() -> None:
    ok, _ = evaluate_recommended(
        **_green(distinct_source_types=1,
                 q2_plus_signal_count=1, deal_attribution_count=1),
    )
    assert ok is True


def test_stale_evidence_blocks() -> None:
    too_old = date.today() - timedelta(days=RECENT_EVIDENCE_WINDOW_DAYS + 30)
    ok, reason = evaluate_recommended(
        **_green(most_recent_signal_date=too_old),
    )
    assert ok is False
    assert "within last 18 months" in reason


def test_left_fund_employment_blocks() -> None:
    ok, _ = evaluate_recommended(**_green(employment_status="left_fund"))
    assert ok is False


def test_major_kill_blocks() -> None:
    ok, reason = evaluate_recommended(**_green(major_kill=True))
    assert ok is False
    assert "major kill" in reason


def test_unknown_reachability_blocks_with_explanation() -> None:
    ok, reason = evaluate_recommended(**_green(cold_reachability_score=None))
    assert ok is False
    assert "cold_reachability_score is unknown" in reason


def test_low_reachability_blocks() -> None:
    ok, reason = evaluate_recommended(
        **_green(cold_reachability_score=COLD_REACHABILITY_MIN - 1),
    )
    assert ok is False
    assert "cold_reachability_score" in reason


def test_warm_path_does_not_demote_recommendation() -> None:
    """PR #10 review: warm_path_available used to add a 'prefer warm
    route' gate fail. Product line is now 'no warm intros, ever' --
    a partner with a warm path is still a valid cold-outreach
    candidate. The kwarg is accepted for back-compat but IGNORED."""
    ok, _reason = evaluate_recommended(**_green(warm_path_available=True))
    assert ok is True


# ----- outcome suppression (Batch 19) -----


def test_meeting_already_booked_blocks_terminally() -> None:
    outcome = {"meeting_booked": True}
    ok, reason = evaluate_recommended(**_green(latest_outcome=outcome))
    assert ok is False
    assert "meeting already booked" in reason


def test_passed_reply_blocks_terminally() -> None:
    outcome = {"reply_type": "passed_not_a_fit"}
    ok, reason = evaluate_recommended(**_green(latest_outcome=outcome))
    assert ok is False
    assert "passed" in reason


def test_wrong_stage_reply_blocks_terminally() -> None:
    outcome = {"reply_type": "wrong_stage"}
    ok, reason = evaluate_recommended(**_green(latest_outcome=outcome))
    assert ok is False
    assert "wrong_stage" in reason


def test_recent_active_outreach_blocks_until_window_elapses() -> None:
    five_days_ago = datetime.utcnow() - timedelta(days=5)
    outcome = {
        "outreach_status": "sent",
        "synced_from_attio_at": five_days_ago,
    }
    ok, _ = evaluate_recommended(
        **_green(latest_outcome=outcome, latest_outcome_window_days=30),
    )
    assert ok is False


def test_old_active_outreach_outside_window_does_not_block() -> None:
    long_ago = datetime.utcnow() - timedelta(days=120)
    outcome = {
        "outreach_status": "sent",
        "synced_from_attio_at": long_ago,
    }
    ok, _ = evaluate_recommended(
        **_green(latest_outcome=outcome, latest_outcome_window_days=30),
    )
    assert ok is True


def test_constants_match_brief_thresholds() -> None:
    """Guard against accidentally drifting these thresholds in a future
    refactor without updating the brief."""
    assert COMPOSITE_MIN == 6.5
    assert ROUND_FIT_MIN == 6.0
    assert LEAD_LIKELIHOOD_MIN == 5.0
    assert COLD_REACHABILITY_MIN == 5.0
    assert RECENT_EVIDENCE_WINDOW_DAYS == 540
