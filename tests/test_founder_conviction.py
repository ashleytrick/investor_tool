"""Unit tests for core/email/founder_conviction.py (Slice 11)."""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.founder_conviction import (
    CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM,
    RISK_HIGH, RISK_LOW, RISK_MEDIUM,
    build_bridge, founder_conviction_from_company,
)


# ----- founder_conviction_from_company -----


def test_normalizes_missing_block_to_defaults() -> None:
    out = founder_conviction_from_company({})
    assert out["non_obvious_belief"] == ""
    assert out["disqualifying_investor_beliefs"] == []
    assert out["proof_points_by_investor_type"] == {}


def test_passes_through_present_fields() -> None:
    cfg = {
        "founder_conviction": {
            "non_obvious_belief": "regulated reporting is the wedge",
            "why_now": "state mandates land Q3",
            "disqualifying_investor_beliefs": ["pre-seed only"],
            "proof_points_by_investor_type": {
                "potential_lead": "ARR + retention proof",
            },
        }
    }
    out = founder_conviction_from_company(cfg)
    assert out["non_obvious_belief"] == "regulated reporting is the wedge"
    assert out["disqualifying_investor_beliefs"] == ["pre-seed only"]
    assert out["proof_points_by_investor_type"]["potential_lead"]


# ----- build_bridge -----


def _signal(*, quality=3, direction="positive", axes=("axis_a",),
            quote="public quote text") -> dict:
    return {
        "quality": quality,
        "direction": direction,
        "axes": list(axes),
        "quote": quote,
    }


def test_no_signals_returns_none() -> None:
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "x"},
        partner_signals=[],
    )
    assert b is None


def test_picks_highest_quality_positive_signal() -> None:
    sigs = [
        _signal(quality=2, quote="weaker"),
        _signal(quality=3, quote="strongest q3 positive"),
        _signal(quality=3, direction="negative", quote="strong but neg"),
    ]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "y"},
        partner_signals=sigs,
    )
    assert b is not None
    # q3 positive preferred over q3 negative.
    assert "strongest q3 positive" in (b.partner_evidence or "")


def test_q3_with_two_axes_is_high_confidence_low_risk() -> None:
    sigs = [_signal(quality=3, axes=("axis_a", "axis_b"))]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "a real belief"},
        partner_signals=sigs,
    )
    assert b is not None
    assert b.confidence == CONFIDENCE_HIGH
    assert b.factual_risk == RISK_LOW


def test_negative_quote_is_high_factual_risk() -> None:
    """A negative quote ('the partner rejects X') is high-risk to
    bridge with -- if the inference is wrong, the email reads as
    combative."""
    sigs = [_signal(direction="negative")]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "x"},
        partner_signals=sigs,
    )
    assert b is not None
    assert b.factual_risk == RISK_HIGH


def test_empty_founder_belief_is_high_risk() -> None:
    """An empty non_obvious_belief means we're guessing both sides of
    the bridge -- the operator MUST review before approval."""
    sigs = [_signal(quality=3)]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": ""},
        partner_signals=sigs,
    )
    assert b is not None
    assert b.factual_risk == RISK_HIGH


def test_q2_quote_is_medium_confidence() -> None:
    sigs = [_signal(quality=2, axes=("axis_a",))]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "real"},
        partner_signals=sigs,
    )
    assert b is not None
    assert b.confidence == CONFIDENCE_MEDIUM


def test_bridge_quote_truncated_for_long_content() -> None:
    long = "x" * 1000
    sigs = [_signal(quote=long)]
    b = build_bridge(
        founder_conviction={"non_obvious_belief": "real"},
        partner_signals=sigs,
    )
    assert b is not None
    # The summary truncates at 200 chars; partner_evidence is the
    # raw quote and may be longer.
    assert len(b.partner_belief) <= 250
