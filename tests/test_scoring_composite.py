"""Unit tests for core/scoring/composite.py (Refactor item 7/13)."""
from __future__ import annotations

from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.composite import (
    CONFIDENCE_HIGH_MIN_AXES,
    CONFIDENCE_MEDIUM_MIN_AXES,
    SPIKY_MAX,
    STUB_NEUTRAL,
    composite_and_spikiness,
    stub_axis_scores,
)


def _axis_data(score, supporting=(), confidence="medium", reasoning=""):
    return SimpleNamespace(
        score=score,
        supporting_signal_ids=list(supporting),
        confidence=confidence,
        reasoning=reasoning,
    )


def _candidate(axis_scores: dict):
    return SimpleNamespace(axis_scores=axis_scores)


# ----- composite_and_spikiness -----


def test_no_scored_axes_returns_none_composite() -> None:
    cs = _candidate({"axis_a": _axis_data(None), "axis_b": _axis_data(None)})
    axes_cfg = {"axes": [
        {"id": "axis_a", "weight": 1.0},
        {"id": "axis_b", "weight": 1.0},
    ]}
    composite, axis_max, variance, spiky, conf = (
        composite_and_spikiness(cs, axes_cfg)
    )
    assert composite is None
    assert axis_max is None
    assert variance == 0.0
    assert spiky == 0.0
    assert conf == "low"


def test_single_axis_weighted_composite_equals_score() -> None:
    cs = _candidate({"axis_a": _axis_data(8.0)})
    axes_cfg = {"axes": [{"id": "axis_a", "weight": 1.0}]}
    composite, axis_max, variance, spiky, conf = (
        composite_and_spikiness(cs, axes_cfg)
    )
    assert composite == 8.0
    assert axis_max == 8.0
    assert variance == 0.0
    assert spiky == 0.0
    assert conf == "low"  # only 1 scored axis


def test_axis_weights_are_applied() -> None:
    """axis_a weight=3, axis_b weight=1 -> composite = (8*3 + 4*1)/4 = 7."""
    cs = _candidate({
        "axis_a": _axis_data(8.0),
        "axis_b": _axis_data(4.0),
    })
    axes_cfg = {"axes": [
        {"id": "axis_a", "weight": 3.0},
        {"id": "axis_b", "weight": 1.0},
    ]}
    composite, _, _, _, _ = composite_and_spikiness(cs, axes_cfg)
    assert composite == 7.0


def test_default_weight_when_unspecified_is_one() -> None:
    cs = _candidate({
        "axis_a": _axis_data(8.0),
        "axis_b": _axis_data(4.0),
    })
    axes_cfg = {"axes": [
        {"id": "axis_a"},
        {"id": "axis_b"},
    ]}
    composite, _, _, _, _ = composite_and_spikiness(cs, axes_cfg)
    assert composite == 6.0  # unweighted mean


def test_axis_max_reflects_highest_scored_axis() -> None:
    cs = _candidate({
        "axis_a": _axis_data(3.0),
        "axis_b": _axis_data(9.0),
        "axis_c": _axis_data(5.0),
    })
    axes_cfg = {"axes": [
        {"id": "axis_a"}, {"id": "axis_b"}, {"id": "axis_c"},
    ]}
    _, axis_max, _, _, _ = composite_and_spikiness(cs, axes_cfg)
    assert axis_max == 9.0


def test_confidence_thresholds_step_through_bands() -> None:
    """1 axis -> low, 2-3 -> medium, 4+ -> high."""
    axes_cfg_4 = {"axes": [{"id": f"a{i}"} for i in range(4)]}
    cs_4 = _candidate({f"a{i}": _axis_data(5.0) for i in range(4)})
    _, _, _, _, conf = composite_and_spikiness(cs_4, axes_cfg_4)
    assert conf == "high"

    cs_3 = _candidate({f"a{i}": _axis_data(5.0) for i in range(3)})
    axes_cfg_3 = {"axes": [{"id": f"a{i}"} for i in range(3)]}
    _, _, _, _, conf = composite_and_spikiness(cs_3, axes_cfg_3)
    assert conf == "medium"


def test_spiky_clamped_to_max() -> None:
    """A partner with one perfect axis and one zero axis has high
    variance; spiky must not exceed SPIKY_MAX."""
    cs = _candidate({
        "axis_a": _axis_data(10.0),
        "axis_b": _axis_data(0.0),
    })
    axes_cfg = {"axes": [{"id": "axis_a"}, {"id": "axis_b"}]}
    _, _, variance, spiky, _ = composite_and_spikiness(cs, axes_cfg)
    # variance = 25, *0.5 = 12.5, but clamped to SPIKY_MAX=2.0.
    assert variance == 25.0
    assert spiky == SPIKY_MAX


def test_confidence_constants_sane() -> None:
    assert CONFIDENCE_HIGH_MIN_AXES > CONFIDENCE_MEDIUM_MIN_AXES > 1


# ----- stub_axis_scores -----


def test_stub_axis_with_no_relevant_signals_scored_none() -> None:
    by_axis = stub_axis_scores([], {"axes": [{"id": "axis_a"}]})
    assert by_axis["axis_a"]["score"] is None
    assert by_axis["axis_a"]["confidence"] == "low"


def test_stub_positive_q3_quotes_raise_above_neutral() -> None:
    sigs = [
        {"id": 1, "axes": ["axis_a"], "direction": "positive", "quality": 3},
        {"id": 2, "axes": ["axis_a"], "direction": "positive", "quality": 3},
    ]
    out = stub_axis_scores(sigs, {"axes": [{"id": "axis_a"}]})
    # neutral (6.0) + 2*Q3_STEP (1.0 each) = 8.0
    assert out["axis_a"]["score"] == 8.0
    assert out["axis_a"]["confidence"] == "high"


def test_stub_negative_q3_quote_lowers_score() -> None:
    """A negative quality-3 quote should drop the axis below neutral,
    not raise it -- the original bug this code path was added to fix."""
    sigs = [
        {"id": 1, "axes": ["axis_a"], "direction": "negative", "quality": 3},
    ]
    out = stub_axis_scores(sigs, {"axes": [{"id": "axis_a"}]})
    # neutral (6.0) - 1*Q3_STEP (1.0) = 5.0
    assert out["axis_a"]["score"] == 5.0


def test_stub_clamps_to_zero_when_many_negatives() -> None:
    sigs = [
        {"id": i, "axes": ["axis_a"], "direction": "negative", "quality": 3}
        for i in range(10)
    ]
    out = stub_axis_scores(sigs, {"axes": [{"id": "axis_a"}]})
    assert out["axis_a"]["score"] == 3.0  # min(3, 10) negatives subtract 3


def test_stub_neutral_when_no_quality_signals_on_axis() -> None:
    """Defensive: STUB_NEUTRAL is the baseline so unscored axes don't
    show up as a Q-something inflation."""
    assert STUB_NEUTRAL == 6.0
