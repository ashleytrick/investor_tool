"""Round-building recommendation model (Slice 10).

Stage 6's `composite_fit_score` answers "how well does this partner
match the founder's thesis?". This module adds two adjacent signals
the operator actually needs when ASSEMBLING A ROUND:

  - investor_role : what role would this partner play in the round?
    A lead-only fund and a credibility co-invest signal are both
    "worth outreach" but for very different reasons; mixing roles
    in a batch produces a real round instead of 25 leads.

  - confidence_band : how confident are we in the recommendation?
    A high-composite partner with one weak signal is a wildcard;
    a medium-composite partner with five strong signals across
    axes is a high-confidence pick.

This is the single pure function: classify(partner_score_context)
returns (investor_role, confidence_band). Stage 6 calls it per
partner and writes both back to partner_score_summaries. Future
batch-selection logic can then balance roles using these labels.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


# Roles, ordered roughly by typical check size + investment authority.
ROLE_POTENTIAL_LEAD: Final[str] = "potential_lead"
ROLE_STRONG_CO_INVESTOR: Final[str] = "strong_co_investor"
ROLE_STRATEGIC_SPECIALIST: Final[str] = "strategic_specialist"
ROLE_CREDIBLE_SIGNAL: Final[str] = "credible_signal_investor"
ROLE_WILDCARD: Final[str] = "wildcard_high_conviction_fit"
ROLE_LOW_PRIORITY: Final[str] = "low_priority"

ALL_ROLES: Final[tuple[str, ...]] = (
    ROLE_POTENTIAL_LEAD, ROLE_STRONG_CO_INVESTOR,
    ROLE_STRATEGIC_SPECIALIST, ROLE_CREDIBLE_SIGNAL,
    ROLE_WILDCARD, ROLE_LOW_PRIORITY,
)


# Confidence bands. `insufficient_evidence` is a fail-safe: when we
# can't even get to `low` (e.g. zero scored axes), we refuse to
# recommend rather than auto-bucket the partner.
CONFIDENCE_HIGH: Final[str] = "high"
CONFIDENCE_MEDIUM: Final[str] = "medium"
CONFIDENCE_LOW: Final[str] = "low"
CONFIDENCE_INSUFFICIENT: Final[str] = "insufficient_evidence"

ALL_CONFIDENCE_BANDS: Final[tuple[str, ...]] = (
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW, CONFIDENCE_INSUFFICIENT,
)


@dataclass(frozen=True)
class RoundClassification:
    investor_role: str
    confidence_band: str


def classify(
    *,
    composite_fit_score: float | None,
    round_fit_score: float | None,
    lead_likelihood_score: float | None,
    spiky_belief_score: float | None,
    axis_max_score: float | None,
    scored_axes_count: int,
    verified_q3_signal_count: int,
    deal_attribution_count: int,
    has_disqualifier: bool,
) -> RoundClassification:
    """Translate Stage 6's per-partner score context into a role +
    confidence band. Deterministic; pure; no DB. Decision rules:

    Role (highest-priority match wins):
      1. has_disqualifier -> low_priority
      2. lead_likelihood >= 7 AND round_fit >= 7 -> potential_lead
      3. lead_likelihood >= 5 AND deal_attribution_count >= 2
                                 -> strong_co_investor
      4. spiky_belief >= 1.5 AND axis_max >= 8
                                 -> strategic_specialist (one
                                    strong axis carries the role)
      5. verified_q3_signal_count >= 2 -> credible_signal_investor
      6. composite_fit_score >= 7 AND scored_axes_count <= 2
                                 -> wildcard_high_conviction_fit
                                    (high score, thin coverage)
      7. otherwise               -> low_priority

    Confidence:
      - scored_axes_count == 0    -> insufficient_evidence
      - scored_axes_count >= 4
        AND composite_fit_score is not None
        AND composite >= 7        -> high
      - scored_axes_count >= 2    -> medium
      - else                       -> low
    """
    # ----- role -----
    if has_disqualifier:
        role = ROLE_LOW_PRIORITY
    elif (
        (lead_likelihood_score or 0) >= 7
        and (round_fit_score or 0) >= 7
    ):
        role = ROLE_POTENTIAL_LEAD
    elif (
        (lead_likelihood_score or 0) >= 5
        and deal_attribution_count >= 2
    ):
        role = ROLE_STRONG_CO_INVESTOR
    elif (
        (spiky_belief_score or 0) >= 1.5
        and (axis_max_score or 0) >= 8
    ):
        role = ROLE_STRATEGIC_SPECIALIST
    elif verified_q3_signal_count >= 2:
        role = ROLE_CREDIBLE_SIGNAL
    elif (
        (composite_fit_score or 0) >= 7
        and scored_axes_count <= 2
    ):
        role = ROLE_WILDCARD
    else:
        role = ROLE_LOW_PRIORITY

    # ----- confidence -----
    if scored_axes_count == 0:
        confidence = CONFIDENCE_INSUFFICIENT
    elif (
        scored_axes_count >= 4
        and composite_fit_score is not None
        and composite_fit_score >= 7
    ):
        confidence = CONFIDENCE_HIGH
    elif scored_axes_count >= 2:
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW

    return RoundClassification(
        investor_role=role, confidence_band=confidence,
    )
