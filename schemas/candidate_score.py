"""Pydantic schema for Stage 6 candidate-scoring LLM output."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class AxisScore(BaseModel):
    score: Optional[float] = None  # 0..10, or None if insufficient data
    supporting_signal_ids: list[int] = []
    confidence: str  # "low" | "medium" | "high"
    reasoning: str


class CandidateScore(BaseModel):
    axis_scores: dict[str, AxisScore]  # keyed by axis_id
