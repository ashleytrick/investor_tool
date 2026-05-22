"""Deterministic round_fit calculation.

SESSION 1 STUB. The real implementation (Session 6) computes round_fit_score
from observable facts only: stage match, check-size overlap, fund activity,
recent relevant deals, partner decision power, with a disqualifier cap. No LLM
ever produces the score. Until then this returns a canned mid-high score.
"""
from __future__ import annotations

from dataclasses import dataclass

STUB = True


@dataclass
class RoundFitResult:
    round_fit_score: float  # 0-10
    round_fit_reasoning: str
    disqualifier_present: bool = False


def compute_round_fit(fund: dict, partner: dict, company_config: dict) -> RoundFitResult:
    """STUB: canned round-fit result. Replace in Session 6."""
    return RoundFitResult(
        round_fit_score=8.0,
        round_fit_reasoning="stub: canned round-fit score (Session 1 vertical slice)",
        disqualifier_present=False,
    )
