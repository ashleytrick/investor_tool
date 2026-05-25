"""Founder conviction + per-partner bridge (Slice 11).

The cold-outreach approach this codebase implements is "founder
conviction first, partner evidence second": every email should lead
with what the founder actually believes (non-obvious, defensible)
and then bridge to what this specific partner has signalled they
also believe.

The Bridge object captures that connection in a structured way:

  founder_claim          : the non-obvious belief from company.yaml
  partner_belief         : what we infer this partner believes, from
                            their verified signals
  partner_evidence       : the specific signal quote / source the
                            inference rests on
  bridge_sentence        : the one-line connector the email opens
                            with ("On the X podcast you said Y --
                            that's exactly why we Z")
  factual_risk           : low / medium / high -- how risky is the
                            factual claim joining the two? An
                            operator looks at this before approving.
  confidence             : low / medium / high -- how confident are
                            we the partner actually holds this
                            belief?

Builders here are RULE-BASED. The LLM still writes the draft body;
this module gives the LLM a structured bridge object to anchor on
+ the review queue something concrete to audit.

Config: company.yaml gains a `founder_conviction` block:

  founder_conviction:
    non_obvious_belief: "..."
    why_now: "..."
    market_change: "..."
    strongest_proof: "..."
    why_this_team_wins: "..."
    disqualifying_investor_beliefs: ["...", "..."]
    # Optional: keyed pool of proof points by investor type.
    proof_points_by_investor_type:
      potential_lead: "..."
      strong_co_investor: "..."
      strategic_specialist: "..."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Bridge:
    founder_claim: str
    partner_belief: str
    partner_evidence: str
    bridge_sentence: str
    factual_risk: str  # low | medium | high
    confidence: str    # low | medium | high


# Risk levels are about the bridge's factual claim, not the email body.
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Confidence levels are about how strongly the partner's signal
# supports the inferred belief.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"


def founder_conviction_from_company(company_cfg: dict) -> dict:
    """Read the founder_conviction block from company.yaml, returning
    a normalized dict with every expected key (defaults to empty
    string / list when missing). Callers can treat fields as always
    present."""
    fc = (company_cfg or {}).get("founder_conviction") or {}
    return {
        "non_obvious_belief": fc.get("non_obvious_belief") or "",
        "why_now": fc.get("why_now") or "",
        "market_change": fc.get("market_change") or "",
        "strongest_proof": fc.get("strongest_proof") or "",
        "why_this_team_wins": fc.get("why_this_team_wins") or "",
        "disqualifying_investor_beliefs":
            list(fc.get("disqualifying_investor_beliefs") or []),
        "proof_points_by_investor_type":
            dict(fc.get("proof_points_by_investor_type") or {}),
    }


def _signal_belief_summary(signal: dict) -> str:
    """Best-effort one-line summary of what a verified signal
    suggests the partner believes. Uses the quoted_text + axis
    relevance + direction. Pure / no LLM."""
    quote = (signal.get("quote") or "").strip()
    if len(quote) > 200:
        quote = quote[:197] + "..."
    direction = (signal.get("direction") or "positive").lower()
    if direction == "negative":
        # Negative quote -> partner does NOT believe X.
        return f"partner explicitly rejects: {quote!r}"
    return f"partner publicly endorses: {quote!r}"


def _classify_confidence(
    quality: int, axis_relevance: list[str],
) -> str:
    """Confidence in the inferred-belief claim. q3 with multiple
    relevant axes is high; q2 with one axis is medium; thinner is
    low."""
    if quality >= 3 and len(axis_relevance) >= 2:
        return CONFIDENCE_HIGH
    if quality >= 2:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _classify_factual_risk(
    *, signal: dict, founder_claim: str,
) -> str:
    """How risky is the factual bridge between the founder's claim
    and what we're inferring this partner believes? Heuristic:

      - empty founder_claim     -> high (we're guessing both ends)
      - direction='negative'    -> high (we're saying 'see, you agree
                                    with us by rejecting THIS' -- if
                                    the inference is wrong, the
                                    email reads as combative)
      - q3 quote on a matched axis -> low
      - everything else         -> medium
    """
    if not founder_claim.strip():
        return RISK_HIGH
    if (signal.get("direction") or "positive").lower() == "negative":
        return RISK_HIGH
    if int(signal.get("quality") or 0) >= 3:
        return RISK_LOW
    return RISK_MEDIUM


def build_bridge(
    *,
    founder_conviction: dict,
    partner_signals: list[dict],
) -> Bridge | None:
    """Construct a Bridge from the founder's conviction config + the
    partner's strongest verified signal. Returns None when the
    partner has no q2+ signals to anchor on.

    Selection rule: take the highest-quality verified signal. Ties
    broken by direction='positive' first (positive evidence is
    safer to lead with than negative).
    """
    founder_claim = founder_conviction.get("non_obvious_belief") or ""
    if not partner_signals:
        return None
    # Prefer positive, q3, axis-tagged signals.
    sorted_signals = sorted(
        partner_signals,
        key=lambda s: (
            -int(s.get("quality") or 0),
            0 if (s.get("direction") or "positive").lower() == "positive" else 1,
            -len(s.get("axes") or []),
        ),
    )
    sig = sorted_signals[0]
    partner_belief = _signal_belief_summary(sig)
    partner_evidence = (sig.get("quote") or "").strip()
    confidence = _classify_confidence(
        quality=int(sig.get("quality") or 0),
        axis_relevance=list(sig.get("axes") or []),
    )
    factual_risk = _classify_factual_risk(
        signal=sig, founder_claim=founder_claim,
    )
    # The bridge sentence template. The LLM's job is to render this
    # into a one-line opener; this is the structured input.
    bridge_sentence = (
        f"You publicly endorsed something that lines up with our "
        f"non-obvious belief: {founder_claim or '(founder belief unset)'}."
    )
    return Bridge(
        founder_claim=founder_claim,
        partner_belief=partner_belief,
        partner_evidence=partner_evidence,
        bridge_sentence=bridge_sentence,
        factual_risk=factual_risk,
        confidence=confidence,
    )
