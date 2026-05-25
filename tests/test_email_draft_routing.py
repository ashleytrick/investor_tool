"""Unit tests for core/email/draft_routing.py.

Slice 1 rewrite: cold-outreach approval model. Stage 7 never auto-
approves; the routing decision computes the operator-visible label
+ blockers for a needs_review draft.
"""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.draft_routing import (
    HINT_NEEDS_REVIEW,
    HINT_QA_FAILED,
    decide_draft_routing,
)


_GOOD_BODY = (
    "On the Distribution podcast you mentioned wedge framing. "
    "Tendril is the wedge productized: $180K ARR with 128% NRR "
    "across four design partners. State mandates land this quarter. "
    "Raising a $3M Seed; first close in 8 weeks. 30 minutes? "
    "https://cal.example/dana-tendril"
)


def _company(**overrides) -> dict:
    cfg = {
        "company": {
            "name": "Tendril",
            "founder_name": "Dana",
            "founder_email": "dana@cal.example",
            "meeting_ask": {
                "preferred_scheduling_link": "https://cal.example/dana-tendril",
            },
        },
    }
    cfg["company"].update(overrides)
    return cfg


def _decide(**overrides):
    base = dict(
        rec_subject="subj",
        rec_body=_GOOD_BODY,
        rec_template_smell="low",
        in_sim_failure_pair=False,
        pctx_recommendation_reasoning="ok",
        pctx_recommended_to_send=True,
        pctx_cold_reachability_score=7.5,
        pctx_partner_email="priya@northbeam.example",
        pctx_do_not_contact=False,
        banned=[],
        company_cfg=_company(),
        allow_example_domains=True,
    )
    base.update(overrides)
    return decide_draft_routing(**base)


# ----- clean needs_review path -----


def test_clean_draft_routes_to_needs_review() -> None:
    """In Slice 1, even a Stage-6-recommended partner with a perfect
    draft only gets to needs_review. The HUMAN approves separately."""
    d = _decide()
    assert d.approval_status_hint == HINT_NEEDS_REVIEW
    assert d.blockers == ()
    assert d.downgraded is False
    # Back-compat aliases still surface the same value.
    assert d.outreach_status == HINT_NEEDS_REVIEW
    assert d.qa_fails == ()


# ----- approval-blocker paths -----


def test_missing_partner_email_blocks_approval() -> None:
    """A draft for a partner with no email STILL gets generated so the
    operator sees who needs Apollo enrichment, but it's labeled
    qa_failed with an explicit blocker so it can't be approved."""
    d = _decide(pctx_partner_email=None)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("partner email is unknown" in b for b in d.blockers)
    assert d.downgraded is True


def test_empty_partner_email_blocks_approval() -> None:
    d = _decide(pctx_partner_email="   ")
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("partner email is unknown" in b for b in d.blockers)


def test_do_not_contact_blocks_approval() -> None:
    """do_not_contact is a HARD blocker that cannot be approved even
    if every other check passes."""
    d = _decide(pctx_do_not_contact=True)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("do_not_contact" in b for b in d.blockers)


def test_template_smell_high_blocks_approval() -> None:
    d = _decide(rec_template_smell="high")
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("template_smell=high" in b for b in d.blockers)


def test_sim_failure_pair_blocks_approval() -> None:
    d = _decide(in_sim_failure_pair=True)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("body similarity" in b for b in d.blockers)


def test_missing_raise_reference_blocks() -> None:
    body = "Hi -- coffee soon?"  # no raise word
    d = _decide(rec_body=body)
    assert d.approval_status_hint == HINT_QA_FAILED
    # check_hard_gates surfaces 'missing explicit raise reference'.
    assert any("raise reference" in b for b in d.blockers)


def test_unknown_reachability_blocks_for_recommended_partner() -> None:
    """Defense in depth: a Stage-6-recommended partner with no
    reachability score still can't be approved."""
    d = _decide(pctx_cold_reachability_score=None)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("cold_reachability_score" in b for b in d.blockers)


def test_hallucinated_scheduling_link_blocks() -> None:
    body = _GOOD_BODY.replace(
        "cal.example/dana-tendril", "calendly.com/wrong-person",
    )
    d = _decide(rec_body=body)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("hallucinated a scheduling URL" in b for b in d.blockers)


def test_founder_email_domain_mismatch_blocks() -> None:
    company = _company(
        founder_email="dana@gmail.com",
        meeting_ask={
            "preferred_scheduling_link": "https://tendril.io/meet/dana",
        },
    )
    body = _GOOD_BODY.replace(
        "cal.example/dana-tendril", "tendril.io/meet/dana",
    )
    d = _decide(company_cfg=company, rec_body=body)
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("founder email domain" in b for b in d.blockers)


def test_example_domain_blocks_without_allow_flag() -> None:
    d = _decide(allow_example_domains=False)
    assert d.approval_status_hint == HINT_QA_FAILED


# ----- warm-path is GONE -----


def test_warm_path_kwarg_accepted_but_ignored() -> None:
    """Slice 1 dropped warm-path. The kwarg is still accepted for
    back-compat with older callers but does nothing -- the partner
    gets a cold needs_review draft like everyone else."""
    d = _decide(pctx_warm_path_available=True)
    # NOT a special warm_path_needed branch; just a normal review.
    assert d.approval_status_hint == HINT_NEEDS_REVIEW
    # No warm-path mention leaked into the reasoning text.
    assert "warm_path" not in d.reasoning.lower()


# ----- reasoning text -----


def test_blocker_reasoning_lists_every_blocker() -> None:
    d = _decide(rec_template_smell="high", pctx_partner_email=None)
    # Both blockers surface in the reasoning string.
    assert "template_smell=high" in d.reasoning
    assert "partner email is unknown" in d.reasoning


def test_reasoning_carries_stage6_context() -> None:
    d = _decide(
        rec_template_smell="high",
        pctx_recommendation_reasoning="Stage 6 said: all criteria met",
    )
    assert "BLOCKERS preventing approval" in d.reasoning
    assert "Stage 6 said" in d.reasoning


# ----- not-recommended path -----


def test_not_recommended_with_clean_draft_still_needs_review() -> None:
    """Stage 6 saying 'not recommended' doesn't auto-reject -- the
    draft still lands in the review queue so the operator can decide.
    The reasoning makes the Stage 6 verdict explicit."""
    d = _decide(
        pctx_recommended_to_send=False,
        pctx_recommendation_reasoning="lead_likelihood_score (1.0) < 5.0",
    )
    assert d.approval_status_hint == HINT_NEEDS_REVIEW
    assert "Stage 6 did not recommend" in d.reasoning
    assert "lead_likelihood_score (1.0)" in d.reasoning


def test_not_recommended_with_blockers_still_qa_failed() -> None:
    d = _decide(
        pctx_recommended_to_send=False, rec_template_smell="high",
    )
    assert d.approval_status_hint == HINT_QA_FAILED
