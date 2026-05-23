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
    template_smell: str = "unscored"


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
