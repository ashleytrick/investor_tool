"""Mostly-deterministic lead_likelihood calculation.

SESSION 1 STUB. The real implementation (Session 6) computes lead_likelihood
from named-as-lead counts, recent board seats, solo-check pattern, and title
seniority, with a follow-on-only penalty. The LLM is used only for explanatory
text, never the score. Until then this returns a canned mid-high score.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

STUB = True


@dataclass
class LeadLikelihoodResult:
    lead_likelihood_score: float  # 0-10
    lead_likelihood_signals: str  # JSON list of evidence rows


def compute_lead_likelihood(partner: dict, deals: list[dict]) -> LeadLikelihoodResult:
    """STUB: canned lead-likelihood result. Replace in Session 6."""
    return LeadLikelihoodResult(
        lead_likelihood_score=7.0,
        lead_likelihood_signals=json.dumps(
            [{"evidence": "stub: canned lead-likelihood (Session 1 vertical slice)"}]
        ),
    )
