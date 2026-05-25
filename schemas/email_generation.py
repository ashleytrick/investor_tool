"""Pydantic schema for Stage 7 email-generation LLM output.

Two variants per partner must use two DIFFERENT strategies (schema-enforced).
One variant is allowed only with limited_variation=True plus a documented
reason.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Strategy = Literal[
    "signal_led",
    "portfolio_led",
    "round_pattern_led",
    "market_shift_led",
    "contrarian_thesis_led",
    "traction_led",
]

# Batch 22 (#607): template_smell is consumed by Stage 7's downgrade
# logic and the batch_qa report; only these four values have meaning.
TemplateSmell = Literal["high", "medium", "low", "unscored"]

# Batch 22 (#608): forbidden phrases that must NOT appear in ANY draft
# (subject, body, alternate body, deck reply, follow-up). Keep in sync
# with scripts/07_generate_emails.py UNIVERSAL_FORBIDDEN.
_UNIVERSAL_FORBIDDEN_LOWER = (
    "building the future of", "would love", "circling back",
    "wanted to reach out", "hope this finds you", "quick question",
    "pressure-test", "compare notes", "thesis chat", "get your feedback",
    "synergy", "game-changing", "excited to",
)


def _contains_forbidden(text: str | None) -> str | None:
    """Return the first forbidden phrase present, or None if clean."""
    if not text:
        return None
    low = text.lower()
    for p in _UNIVERSAL_FORBIDDEN_LOWER:
        if p in low:
            return p
    return None


class EmailVariant(BaseModel):
    strategy: Strategy
    subject: str = Field(..., min_length=1, max_length=80)
    # Brief: 4 sentences max -> ~40 chars min is a defensive lower bound that
    # catches "" and "..." while not policing legitimate short drafts.
    body: str = Field(..., min_length=40)
    # Per brief STEP 3/4: conversion_hypothesis and likely_objection are
    # required FOR THE RECOMMENDED VARIANT only. Alternates may leave them
    # empty. The check_recommended_variant_complete validator on EmailOutput
    # enforces non-empty on whichever variant is named recommended.
    conversion_hypothesis: str = ""
    likely_objection: str = ""
    objection_preempted: bool = False
    preemption_line: Optional[str] = None
    # Batch 22 (#607): bound template_smell to the four meaningful values.
    template_smell: TemplateSmell = "unscored"

    @field_validator("body")
    @classmethod
    def body_no_em_dash_or_exclamation(cls, v: str) -> str:
        """Batch 22 (#612): block em dashes and exclamation marks at the
        schema layer (alternate variants used to flow through Stage 7's
        check_hard_gates only for the recommended draft). The brief's
        prose: 'No em dashes. No exclamation marks.'"""
        if "—" in v:
            raise ValueError("body must not contain em dashes (—)")
        if "!" in v:
            raise ValueError("body must not contain exclamation marks")
        forbidden = _contains_forbidden(v)
        if forbidden:
            raise ValueError(f"body contains forbidden phrase: {forbidden!r}")
        return v

    @field_validator("subject")
    @classmethod
    def subject_constraints(cls, v: str) -> str:
        """Brief STEP 2: subject is 'Not a question. 5 words maximum.
        Specific.' The hard 80-char + min_length=1 floor stays; this
        validator enforces the structural rules so a live LLM ignoring
        them retries instead of producing a non-conforming subject."""
        stripped = v.strip()
        if stripped.endswith("?"):
            raise ValueError(
                f"subject must not be a question; got {v!r}"
            )
        word_count = len(stripped.split())
        if word_count > 5:
            raise ValueError(
                f"subject must be <= 5 words (brief STEP 2); got "
                f"{word_count} words: {v!r}"
            )
        # Batch 22 (#374): subjects must also avoid the universal
        # forbidden phrases. A "Quick question" subject used to slip
        # through because the hard gate only inspected bodies.
        forbidden = _contains_forbidden(v)
        if forbidden:
            raise ValueError(
                f"subject contains forbidden phrase: {forbidden!r}"
            )
        return v

    @model_validator(mode="after")
    def preemption_consistency(self) -> "EmailVariant":
        """objection_preempted=True requires a non-empty preemption_line,
        and vice versa. The pair must agree so downstream consumers can
        trust either field as the source of truth."""
        line = (self.preemption_line or "").strip()
        if self.objection_preempted and not line:
            raise ValueError(
                "objection_preempted=True requires a non-empty preemption_line"
            )
        if line and not self.objection_preempted:
            raise ValueError(
                "preemption_line is set but objection_preempted is False; "
                "set objection_preempted=True or clear the line"
            )
        return self


class EmailOutput(BaseModel):
    variants: list[EmailVariant]
    recommended_variant_strategy: Optional[Strategy] = None
    recommendation_reasoning: str = Field(..., min_length=1)
    limited_variation: bool = False
    limited_variation_reason: Optional[str] = None
    # deck + followup are required outputs PER PARTNER per the brief; an
    # empty string from the LLM should retry, not flow into the CSV.
    deck_request_response: str = Field(..., min_length=1)
    followup_draft: str = Field(..., min_length=1)

    @field_validator("deck_request_response", "followup_draft")
    @classmethod
    def deck_followup_no_forbidden_or_em(cls, v: str) -> str:
        """Batch 22 (#373/#608): the deck-request reply and the follow-up
        draft used to flow straight into the CSV / Attio sync without any
        forbidden-phrase or em-dash gate -- those checks only fired on
        the recommended email body. Apply the same baseline."""
        if "—" in v:
            raise ValueError("deck/followup must not contain em dashes (—)")
        if "!" in v:
            raise ValueError("deck/followup must not contain exclamation marks")
        forbidden = _contains_forbidden(v)
        if forbidden:
            raise ValueError(
                f"deck/followup contains forbidden phrase: {forbidden!r}"
            )
        return v

    @field_validator("variants")
    @classmethod
    def variants_count_and_uniqueness(cls, v: list[EmailVariant]) -> list[EmailVariant]:
        if len(v) not in (0, 1, 2):
            raise ValueError("Must produce 0, 1, or 2 variants")
        if len(v) == 2 and v[0].strategy == v[1].strategy:
            raise ValueError("If producing 2 variants, they must use different strategies")
        return v

    @model_validator(mode="after")
    def check_limited_variation(self) -> "EmailOutput":
        if len(self.variants) < 2:
            if not self.limited_variation:
                raise ValueError("Fewer than 2 variants requires limited_variation=True")
            if not self.limited_variation_reason:
                raise ValueError(
                    "limited_variation_reason required when limited_variation=True"
                )
        return self

    @model_validator(mode="after")
    def check_recommended_variant_complete(self) -> "EmailOutput":
        """The variant named in recommended_variant_strategy must have
        non-empty conversion_hypothesis and likely_objection. Alternates
        may leave them blank."""
        if self.recommended_variant_strategy is None:
            return self
        for v in self.variants:
            if v.strategy == self.recommended_variant_strategy:
                if not (v.conversion_hypothesis or "").strip():
                    raise ValueError(
                        "recommended variant must have non-empty conversion_hypothesis"
                    )
                if not (v.likely_objection or "").strip():
                    raise ValueError(
                        "recommended variant must have non-empty likely_objection"
                    )
        return self

    @model_validator(mode="after")
    def check_recommended_in_variants(self) -> "EmailOutput":
        """Finding 1: previously the LLM could recommend a strategy it did
        not include in `variants` and the downstream code silently dropped
        the partner. Now the schema enforces that recommended_variant_strategy
        names a real variant (or is None iff variants is empty)."""
        present = {v.strategy for v in self.variants}
        if self.variants:
            if self.recommended_variant_strategy is None:
                raise ValueError(
                    "recommended_variant_strategy must be set when variants "
                    "is non-empty"
                )
            if self.recommended_variant_strategy not in present:
                raise ValueError(
                    f"recommended_variant_strategy "
                    f"{self.recommended_variant_strategy!r} not in returned "
                    f"variants {sorted(present)}"
                )
        elif self.recommended_variant_strategy is not None:
            raise ValueError(
                "recommended_variant_strategy must be None when variants is empty"
            )
        return self
