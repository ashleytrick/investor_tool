"""Pydantic schema for Stage 4 partner-signal LLM output.

Stage 4 produces thesis signals (per axis) and cold reachability evidence only.
round_fit and lead_likelihood are NOT here: they are deterministic, computed in
Stage 6 from observable facts.

Enum / range tightening: source_type, signal_direction, confidence, and
direction are Literal-bounded; cold_reachability_partial_score is 0..10.
An LLM returning anything else raises ValidationError -> the LLM client
retries with a stricter prompt instead of silently storing junk.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator

SourceType = Literal[
    "podcast", "blog", "essay", "social", "fund_site",
    "funding_announcement", "interview", "substack",
    "reachability_evidence",
]


class Signal(BaseModel):
    quoted_text: str
    source_url: HttpUrl
    source_type: SourceType
    quote_date: Optional[date] = None
    axis_relevance: list[str]  # must be non-empty
    signal_direction: Literal["positive", "negative"]
    confidence: Literal["high", "medium", "low"]

    @field_validator("axis_relevance")
    @classmethod
    def axis_relevance_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("axis_relevance must be non-empty")
        return v


class EvidenceSignal(BaseModel):
    """Cold reachability evidence."""

    evidence: str
    source_url: HttpUrl
    direction: Literal["positive", "negative"]


class PartnerSignalsOutput(BaseModel):
    signals: list[Signal]
    reachability_signals: list[EvidenceSignal]
    cold_reachability_partial_score: Optional[float] = Field(
        default=None, ge=0.0, le=10.0,
    )
    cold_reachability_reasoning: Optional[str] = None
