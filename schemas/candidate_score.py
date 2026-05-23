"""Pydantic schema for Stage 6 candidate-scoring LLM output.

Bounds are enforced: a live LLM returning score=87 or score=-3 raises
ValidationError, which the LLM client retries up to 3 times with a stricter
prompt -- preferable to silently flowing garbage into composite_fit_score.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class AxisScore(BaseModel):
    score: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    supporting_signal_ids: list[int] = []
    confidence: Literal["low", "medium", "high"]
    reasoning: str


class CandidateScore(BaseModel):
    axis_scores: dict[str, AxisScore]  # keyed by axis_id
