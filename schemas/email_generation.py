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
    subject: str = Field(..., max_length=80)
    body: str
    conversion_hypothesis: str
    likely_objection: str
    objection_preempted: bool
    preemption_line: Optional[str] = None
    template_smell: str = "unscored"


class EmailOutput(BaseModel):
    variants: list[EmailVariant]
    recommended_variant_strategy: Optional[Strategy] = None
    recommendation_reasoning: str
    limited_variation: bool = False
    limited_variation_reason: Optional[str] = None
    deck_request_response: str
    followup_draft: str

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
