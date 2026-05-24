"""Pydantic schema for Stage 5 signal-quality LLM output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SignalQuality(BaseModel):
    signal_quality_score: Literal[0, 1, 2, 3]
    # quality_reasoning is what the operator reads when auditing why a signal
    # scored low. An empty string makes that audit impossible.
    quality_reasoning: str = Field(..., min_length=1)
