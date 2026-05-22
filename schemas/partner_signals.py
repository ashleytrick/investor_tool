"""Pydantic schema for Stage 4 partner-signal LLM output.

Stage 4 produces thesis signals (per axis) and cold reachability evidence only.
round_fit and lead_likelihood are NOT here: they are deterministic, computed in
Stage 6 from observable facts.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, HttpUrl, field_validator


class Signal(BaseModel):
    quoted_text: str
    source_url: HttpUrl
    source_type: str  # podcast, blog, essay, social, fund_site, funding_announcement, interview
    quote_date: Optional[date] = None
    axis_relevance: list[str]  # must be non-empty
    signal_direction: str  # "positive" or "negative"
    confidence: str  # "high", "medium", "low"

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
    direction: str


class PartnerSignalsOutput(BaseModel):
    signals: list[Signal]
    reachability_signals: list[EvidenceSignal]
    cold_reachability_partial_score: Optional[float] = None
    cold_reachability_reasoning: Optional[str] = None
