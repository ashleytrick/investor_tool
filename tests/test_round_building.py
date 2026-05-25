"""Unit tests for core/scoring/round_building.py (Slice 10)."""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.round_building import (
    ALL_CONFIDENCE_BANDS,
    ALL_ROLES,
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    ROLE_CREDIBLE_SIGNAL,
    ROLE_LOW_PRIORITY,
    ROLE_POTENTIAL_LEAD,
    ROLE_STRATEGIC_SPECIALIST,
    ROLE_STRONG_CO_INVESTOR,
    ROLE_WILDCARD,
    classify,
)


def _classify(**over):
    base = dict(
        composite_fit_score=5.0,
        round_fit_score=5.0,
        lead_likelihood_score=3.0,
        spiky_belief_score=0.5,
        axis_max_score=6.0,
        scored_axes_count=3,
        verified_q3_signal_count=0,
        deal_attribution_count=0,
        has_disqualifier=False,
    )
    base.update(over)
    return classify(**base)


# ----- role -----


def test_disqualifier_routes_to_low_priority() -> None:
    r = _classify(
        has_disqualifier=True,
        lead_likelihood_score=10, round_fit_score=10,
        composite_fit_score=10,
    )
    assert r.investor_role == ROLE_LOW_PRIORITY


def test_high_lead_and_round_fit_is_potential_lead() -> None:
    r = _classify(lead_likelihood_score=8, round_fit_score=8)
    assert r.investor_role == ROLE_POTENTIAL_LEAD


def test_solid_lead_with_two_deals_is_strong_co_investor() -> None:
    r = _classify(
        lead_likelihood_score=6, round_fit_score=5,
        deal_attribution_count=3,
    )
    assert r.investor_role == ROLE_STRONG_CO_INVESTOR


def test_high_spiky_and_axis_max_is_strategic_specialist() -> None:
    """A partner with one strong axis carries the role even if
    aggregate is medium."""
    r = _classify(
        composite_fit_score=6, lead_likelihood_score=3,
        spiky_belief_score=1.8, axis_max_score=9,
    )
    assert r.investor_role == ROLE_STRATEGIC_SPECIALIST


def test_two_q3_signals_alone_is_credible_signal() -> None:
    r = _classify(
        composite_fit_score=5, lead_likelihood_score=2,
        verified_q3_signal_count=3,
    )
    assert r.investor_role == ROLE_CREDIBLE_SIGNAL


def test_high_composite_with_thin_axes_is_wildcard() -> None:
    """High aggregate but only 1-2 scored axes -- not enough breadth
    for a confident pick, but the conviction warrants outreach."""
    r = _classify(
        composite_fit_score=8, lead_likelihood_score=2,
        verified_q3_signal_count=0, scored_axes_count=2,
    )
    assert r.investor_role == ROLE_WILDCARD


def test_low_aggregate_with_no_signals_is_low_priority() -> None:
    r = _classify(
        composite_fit_score=4, lead_likelihood_score=2,
        verified_q3_signal_count=0, scored_axes_count=4,
    )
    assert r.investor_role == ROLE_LOW_PRIORITY


# ----- confidence -----


def test_zero_scored_axes_insufficient() -> None:
    r = _classify(scored_axes_count=0)
    assert r.confidence_band == CONFIDENCE_INSUFFICIENT


def test_four_axes_high_composite_is_high_confidence() -> None:
    r = _classify(scored_axes_count=4, composite_fit_score=7.5)
    assert r.confidence_band == CONFIDENCE_HIGH


def test_two_axes_is_medium() -> None:
    r = _classify(scored_axes_count=2, composite_fit_score=6)
    assert r.confidence_band == CONFIDENCE_MEDIUM


def test_one_axis_is_low() -> None:
    r = _classify(scored_axes_count=1, composite_fit_score=8)
    assert r.confidence_band == CONFIDENCE_LOW


def test_four_axes_low_composite_is_medium_not_high() -> None:
    """High confidence requires BOTH breadth (>= 4 axes) AND a
    composite >= 7. Otherwise it's medium."""
    r = _classify(scored_axes_count=5, composite_fit_score=5)
    assert r.confidence_band == CONFIDENCE_MEDIUM


# ----- constant sanity -----


def test_all_roles_and_bands_self_consistent() -> None:
    for r in (
        ROLE_POTENTIAL_LEAD, ROLE_STRONG_CO_INVESTOR,
        ROLE_STRATEGIC_SPECIALIST, ROLE_CREDIBLE_SIGNAL,
        ROLE_WILDCARD, ROLE_LOW_PRIORITY,
    ):
        assert r in ALL_ROLES
    for c in (
        CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
        CONFIDENCE_INSUFFICIENT,
    ):
        assert c in ALL_CONFIDENCE_BANDS
