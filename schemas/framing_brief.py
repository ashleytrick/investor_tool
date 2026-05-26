"""Pydantic schema for the meeting-prep framing-brief builder.

The framing brief synthesizes everything (partner signals, fund
context, the objection map just built, and the company.yaml block)
into a one-page "how to tell your story to *this* partner"
recommendation.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class FramingBriefV1(BaseModel):
    partner_id: str = Field(..., min_length=1)

    # The lead -- the angle / metric / narrative frame to open with.
    # Empty only allowed when insufficient_evidence=True.
    lead_with: str = ""

    # 2-3 angles to amplify, each one a short imperative sentence.
    # Empty list allowed only when insufficient_evidence=True.
    amplify: list[str] = Field(default_factory=list)

    # Objections to preempt unprompted -- typically drawn from the
    # objection map's high-severity entries.
    address_unprompted: list[str] = Field(default_factory=list)

    # Patterns this partner has criticized publicly; framings to
    # avoid leading with.
    do_not_lead_with: list[str] = Field(default_factory=list)

    # The closing question -- a single, specific ask that demonstrates
    # research and opens a real conversation. Empty only allowed when
    # insufficient_evidence=True.
    question_to_ask_them: str = ""

    # Signal ids that back any partner-specific claim in this brief.
    # Empty when insufficient_evidence=True (or when every angle is
    # company-side rather than partner-side).
    citing_signal_ids: list[int] = Field(default_factory=list)

    insufficient_evidence: bool = False
    notes: str = ""

    @model_validator(mode="after")
    def shape_matches_evidence_flag(self) -> "FramingBriefV1":
        """If evidence is insufficient, the brief must NOT make
        partner-specific recommendations (those would be fabricated).
        Symmetric: when evidence is sufficient, the brief MUST have a
        lead and a question -- empty strings there mean the LLM
        produced nothing useful and the operator should know."""
        if self.insufficient_evidence:
            if self.lead_with or self.amplify or self.question_to_ask_them:
                raise ValueError(
                    "insufficient_evidence=True must not be paired "
                    "with lead_with / amplify / question_to_ask_them; "
                    "either flip the flag or drop the recommendations"
                )
            return self
        if not self.lead_with.strip():
            raise ValueError(
                "insufficient_evidence=False requires a non-empty "
                "lead_with -- empty means the LLM produced nothing"
            )
        if not self.question_to_ask_them.strip():
            raise ValueError(
                "insufficient_evidence=False requires a non-empty "
                "question_to_ask_them"
            )
        if not self.amplify:
            raise ValueError(
                "insufficient_evidence=False requires at least one "
                "amplify item"
            )
        return self
