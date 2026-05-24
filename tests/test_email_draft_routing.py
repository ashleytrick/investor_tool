"""Unit tests for core/email/draft_routing.py (Refactor item 14)."""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.draft_routing import (
    STATUS_DRAFT,
    STATUS_READY,
    STATUS_WARM_PATH,
    decide_draft_routing,
)


# A realistic, passing draft body shared across tests. Mentions "raising"
# and contains a scheduling link that matches the workspace config below,
# so check_hard_gates + the scheduling-link hallucination check both pass.
_GOOD_BODY = (
    "On the Distribution podcast you mentioned wedge framing. "
    "Tendril is the wedge productized: $180K ARR with 128% NRR "
    "across four design partners. State mandates land this quarter. "
    "Raising a $3M Seed; first close in 8 weeks. 30 minutes? "
    "https://cal.example/dana-tendril"
)


def _company(**overrides) -> dict:
    """Default config matching the GOOD_BODY scheduling link."""
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
        pctx_warm_path_available=False,
        pctx_cold_reachability_score=7.5,
        banned=[],
        company_cfg=_company(),
        allow_example_domains=True,  # fixture default; .example links pass
    )
    base.update(overrides)
    return decide_draft_routing(**base)


# ----- ready_to_send happy path -----


def test_recommended_partner_with_clean_draft_goes_ready() -> None:
    d = _decide()
    assert d.outreach_status == STATUS_READY
    assert d.qa_fails == ()
    assert d.downgraded is False


# ----- warm-path takes precedence -----


def test_warm_path_available_routes_to_warm_path_needed() -> None:
    d = _decide(pctx_warm_path_available=True)
    assert d.outreach_status == STATUS_WARM_PATH
    assert "warm_path_available=TRUE" in d.reasoning
    assert d.downgraded is False


def test_warm_path_wins_even_when_qa_fails_exist() -> None:
    """warm_path is the first branch in the decision order -- a Stage 6
    recommendation with warm_path set should still route to warm even
    if the draft has hard-gate failures, so we don't waste a cold draft
    on someone we have a warm intro for."""
    d = _decide(
        pctx_warm_path_available=True,
        rec_body="Hi -- coffee soon?",  # would normally fail QA
    )
    assert d.outreach_status == STATUS_WARM_PATH


# ----- downgrade path -----


def test_template_smell_high_downgrades_to_draft() -> None:
    d = _decide(rec_template_smell="high")
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
    assert "template_smell=high" in d.reasoning


def test_sim_failure_pair_downgrades_to_draft() -> None:
    d = _decide(in_sim_failure_pair=True)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
    assert "body similarity" in d.reasoning


def test_missing_raise_reference_downgrades() -> None:
    body = "Hi -- coffee soon?"  # no raise word
    d = _decide(rec_body=body)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True


def test_unknown_reachability_blocks_ready_send() -> None:
    """Defense in depth: even if Stage 6 marked recommended, missing
    reachability score should still downgrade -- the dual-write
    protection from Batch 37 #42."""
    d = _decide(pctx_cold_reachability_score=None)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
    assert "cold_reachability_score is unknown" in d.reasoning


def test_hallucinated_scheduling_link_downgrades() -> None:
    """Body contains calendly.com link but workspace config points to
    cal.example -- LLM may have invented the wrong URL."""
    body = _GOOD_BODY.replace(
        "cal.example/dana-tendril", "calendly.com/wrong-person",
    )
    d = _decide(rec_body=body)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
    assert "hallucinated a scheduling URL" in d.reasoning


def test_downgrade_reasoning_carries_stage6_context() -> None:
    d = _decide(
        rec_template_smell="high",
        pctx_recommendation_reasoning="Stage 6 said: all criteria met",
    )
    # Both the Stage 7 downgrade reason AND the Stage 6 context land
    # in the CSV reasoning so the operator sees both layers.
    assert "DOWNGRADED by Stage 7 QA" in d.reasoning
    assert "Stage 6 said" in d.reasoning


# ----- not-recommended path -----


def test_not_recommended_partner_routes_to_draft() -> None:
    d = _decide(pctx_recommended_to_send=False)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is False  # NOT a downgrade -- Stage 6 already said no


def test_not_recommended_partner_reasoning_passes_through() -> None:
    d = _decide(
        pctx_recommended_to_send=False,
        pctx_recommendation_reasoning="lead_likelihood_score (1.0) < 5.0",
    )
    # Verbatim pass-through; not wrapped with "DOWNGRADED" prefix.
    assert d.reasoning == "lead_likelihood_score (1.0) < 5.0"


def test_founder_email_domain_mismatch_downgrades_recommended() -> None:
    """gmail.com founder_email but the scheduling link's primary
    domain is tendril.io -> mismatch -> downgrade.

    (Reserved TLDs like .example fall through to the founder email's
    domain in primary-domain detection, so a real-looking host is
    needed to exercise the mismatch path.)
    """
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
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
    assert "founder email domain" in d.reasoning


def test_example_domain_blocks_without_allow_flag() -> None:
    """allow_example_domains=False + .example scheduling link =>
    production guard rejects the draft."""
    d = _decide(allow_example_domains=False)
    assert d.outreach_status == STATUS_DRAFT
    assert d.downgraded is True
