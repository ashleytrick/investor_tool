"""Unit tests for core/email/batch_qa.py (Refactor item 7/14)."""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.batch_qa import (
    SIM_BODY_HARD,
    SOFT_CTA_PHRASES,
    UNIVERSAL_FORBIDDEN,
    check_hard_gates,
    evaluate_batch,
    template_smell_judge,
)


_GOOD_BODY = (
    "On your podcast you mentioned compliance reporting is the wedge. "
    "We are raising a $3M Seed; first close in 8 weeks. "
    "I would like 30 minutes to walk you through the round: "
    "https://cal.example/dana-tendril"
)


# ----- check_hard_gates -----


def test_clean_draft_passes_all_gates() -> None:
    assert check_hard_gates({"body": _GOOD_BODY}) == []


def test_unfilled_placeholder_fails() -> None:
    body = _GOOD_BODY + " Available {TIME_1} or {TIME_2}."
    fails = check_hard_gates({"body": body})
    assert any("placeholder" in f for f in fails)


def test_missing_raise_reference_fails() -> None:
    body = "Hi Marcus, lots of admiration for your portfolio. Coffee soon?"
    fails = check_hard_gates({"body": body})
    assert any("raise reference" in f for f in fails)


def test_word_boundary_raise_reference_passes_punctuated() -> None:
    body = _GOOD_BODY.replace("Seed", "Series A")
    assert "missing explicit raise reference in body" not in check_hard_gates(
        {"body": body},
    )


def test_soft_cta_fails() -> None:
    for cta in SOFT_CTA_PHRASES:
        body = _GOOD_BODY + f" Would love a {cta}."
        fails = check_hard_gates({"body": body})
        assert any("soft CTA" in f for f in fails), cta


def test_universal_forbidden_phrase_fails() -> None:
    for phrase in UNIVERSAL_FORBIDDEN[:3]:
        body = _GOOD_BODY + f" Just wanted to say: {phrase}."
        fails = check_hard_gates({"body": body})
        assert any("forbidden phrase" in f for f in fails)


def test_founder_voice_banned_phrase_layered_on_universal() -> None:
    body = _GOOD_BODY + " Excited to disrupt the space."
    fails = check_hard_gates({"body": body}, banned=("disrupt the space",))
    assert any("disrupt the space" in f for f in fails)


def test_em_dash_fails() -> None:
    body = _GOOD_BODY + " — but please reply by Friday."
    fails = check_hard_gates({"body": body})
    assert any("em dash" in f for f in fails)


def test_exclamation_fails() -> None:
    body = _GOOD_BODY + " Excited!"
    fails = check_hard_gates({"body": body})
    assert any("exclamation" in f for f in fails)


# ----- template_smell_judge -----


def test_smell_low_when_no_neighbors() -> None:
    smell, mass, too_sim = template_smell_judge("hello", [])
    assert smell == "low"
    assert mass is False
    assert too_sim is False


def test_smell_high_when_neighbor_is_near_duplicate() -> None:
    body = "On the podcast you said something. We are raising a Seed."
    neighbor = body  # identical content -> body_sim = 1.0
    smell, _, too_sim = template_smell_judge(body, [neighbor])
    assert smell == "high"
    assert too_sim is True


def test_smell_low_for_distinct_drafts() -> None:
    body = "Reporting wedge productized. Raising $3M Seed."
    neighbors = [
        "DevFin reconciliation infrastructure. Raising Seed.",
        "Founder sales is how we got design partners.",
    ]
    smell, _, _ = template_smell_judge(body, neighbors)
    assert smell == "low"


# ----- evaluate_batch -----


def test_batch_passes_when_drafts_are_distinct() -> None:
    # Two recommended drafts with no shared content beyond the
    # mandatory raise reference + scheduling link -- token-set
    # similarity stays well under SIM_BODY_HARD = 0.82.
    drafts = [
        {"partner_id": "a", "strategy": "signal_led",
         "body": (
             "On the Distribution podcast you said wedge framing matters. "
             "Tendril is at $180K ARR. Raising a $3M Seed; first close "
             "in 8 weeks. 30 minutes? https://cal.example/dana-tendril"
         ),
         "subject": "Reporting wedge"},
        {"partner_id": "b", "strategy": "portfolio_led",
         "body": (
             "Comply.io and LedgerKit sit one layer down from where we "
             "operate. Four design partners. Raising a Series A; first "
             "close 8 weeks out. Worth 30 mins? "
             "https://cal.example/dana-tendril"
         ),
         "subject": "Adjacent to Comply"},
    ]
    result = evaluate_batch(drafts, drafts)
    assert result["passed"] is True
    assert result["hard_fail_reasons"] == []


def test_batch_fails_on_duplicate_recommended_bodies() -> None:
    body = _GOOD_BODY
    drafts = [
        {"partner_id": "a", "strategy": "signal_led",
         "body": body, "subject": "s a"},
        {"partner_id": "b", "strategy": "signal_led",
         "body": body, "subject": "s b"},
    ]
    result = evaluate_batch(drafts, drafts)
    assert result["passed"] is False
    assert result["similarity_failure_count"] >= 1


def test_batch_marks_template_smell_high_on_duplicates() -> None:
    body = _GOOD_BODY
    drafts = [
        {"partner_id": "a", "strategy": "signal_led",
         "body": body, "subject": "s a"},
        {"partner_id": "b", "strategy": "signal_led",
         "body": body, "subject": "s b"},
    ]
    result = evaluate_batch(drafts, drafts)
    assert result["template_smell_high_count"] == 2
    for d in drafts:
        assert d["template_smell"] == "high"


def test_batch_warns_on_strategy_concentration() -> None:
    drafts = [
        {"partner_id": f"p{i}", "strategy": "signal_led",
         "body": _GOOD_BODY + f" v{i}", "subject": f"s {i}"}
        for i in range(4)
    ]
    result = evaluate_batch(drafts, drafts)
    assert any("signal_led" in w for w in result["warnings"])


def test_batch_counts_raise_reference_missing() -> None:
    drafts = [
        {"partner_id": "a", "strategy": "signal_led",
         "body": "Hi -- coffee?", "subject": "subj a"},  # no raise
        {"partner_id": "b", "strategy": "signal_led",
         "body": _GOOD_BODY + " v2", "subject": "subj b"},
    ]
    result = evaluate_batch(drafts, drafts)
    assert result["raise_reference_missing_count"] == 1
    assert result["passed"] is False


def test_sim_thresholds_are_documented_constants() -> None:
    assert 0 < SIM_BODY_HARD < 1
