"""Honest attribution status helpers (Slice 6).

Every `deal_attributions` row Stage 3 writes carries a `match_status`
and `matched_by`. Stage 6's lead-likelihood scoring filters on
`match_status` so:

  - `confirmed`         counts toward scoring
  - `likely` AND match_confidence >= STRONG_LIKELY_MIN counts toward
                         scoring
  - `likely` weaker than threshold is context only, not scoring
  - `ambiguous`         never counts; goes to the review queue
  - `rejected`          never counts
  - `unmatched`         never counts (Stage 3 couldn't resolve at all)

`matched_by` records HOW the match was made so a future operator
audit ("why did this Q3 evidence count toward lead_likelihood?")
can reconstruct the decision.
"""
from __future__ import annotations

from typing import Final


STATUS_CONFIRMED: Final[str] = "confirmed"
STATUS_LIKELY: Final[str] = "likely"
STATUS_AMBIGUOUS: Final[str] = "ambiguous"
STATUS_REJECTED: Final[str] = "rejected"
STATUS_UNMATCHED: Final[str] = "unmatched"

ALL_MATCH_STATUSES: Final[frozenset[str]] = frozenset({
    STATUS_CONFIRMED, STATUS_LIKELY, STATUS_AMBIGUOUS,
    STATUS_REJECTED, STATUS_UNMATCHED,
})


MATCHED_BY_EXACT: Final[str] = "exact"
MATCHED_BY_DOMAIN: Final[str] = "domain"
MATCHED_BY_FUND_NAME: Final[str] = "fund_name"
MATCHED_BY_PARTNER_NAME: Final[str] = "partner_name"
MATCHED_BY_LLM: Final[str] = "llm"
MATCHED_BY_MANUAL: Final[str] = "manual"

ALL_MATCHED_BY: Final[frozenset[str]] = frozenset({
    MATCHED_BY_EXACT, MATCHED_BY_DOMAIN, MATCHED_BY_FUND_NAME,
    MATCHED_BY_PARTNER_NAME, MATCHED_BY_LLM, MATCHED_BY_MANUAL,
})


# Match-confidence band for `likely` to count as scoring evidence.
# Below this, the row exists as audit context only.
STRONG_LIKELY_MIN: Final[float] = 0.85


# Statuses that count toward Stage 6 lead_likelihood scoring.
SCORING_STATUSES: Final[frozenset[str]] = frozenset({
    STATUS_CONFIRMED, STATUS_LIKELY,
})


def counts_toward_scoring(
    *, match_status: str | None, match_confidence: float | None,
) -> bool:
    """Single source of truth for Stage 6's filter. A row counts if:
      - status is `confirmed` (any confidence), OR
      - status is `likely` AND confidence >= STRONG_LIKELY_MIN.

    Rejected / ambiguous / unmatched / unknown statuses never count.
    """
    if not match_status:
        return False
    if match_status == STATUS_CONFIRMED:
        return True
    if match_status == STATUS_LIKELY:
        return (match_confidence or 0.0) >= STRONG_LIKELY_MIN
    return False


def classify_from_candidates(
    *,
    chosen_fund_id: str | None,
    candidates: list[dict],
    ambiguity_delta: float,
    fuzzy_threshold: float,
) -> tuple[str, str | None]:
    """Translate Stage 3's match_fund_with_candidates output into
    (match_status, matched_by).

    Decision rules:
      - chosen_fund_id is None        -> unmatched / None
      - top candidate score == 1.0    -> confirmed / exact
      - 2nd candidate within delta    -> ambiguous / fund_name
      - top score >= fuzzy_threshold  -> likely / fund_name
      - otherwise                     -> unmatched / None

    These map the deterministic-matcher output to the user-facing
    vocabulary. `matched_by` defaults to `fund_name` for fuzzy
    matches; callers that resolved via a different path (domain,
    LLM, manual) override.
    """
    if chosen_fund_id is None or not candidates:
        return STATUS_UNMATCHED, None
    top = candidates[0]
    top_score = float(top.get("score", 0.0))
    if top_score >= 1.0:
        return STATUS_CONFIRMED, MATCHED_BY_EXACT
    if len(candidates) >= 2:
        second_score = float(candidates[1].get("score", 0.0))
        if (top_score - second_score) < ambiguity_delta:
            return STATUS_AMBIGUOUS, MATCHED_BY_FUND_NAME
    if top_score >= fuzzy_threshold:
        return STATUS_LIKELY, MATCHED_BY_FUND_NAME
    return STATUS_UNMATCHED, None
