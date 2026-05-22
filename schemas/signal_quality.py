"""Pydantic schema for Stage 5 signal-quality LLM output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SignalQuality(BaseModel):
    signal_quality_score: Literal[0, 1, 2, 3]
    quality_reasoning: str
