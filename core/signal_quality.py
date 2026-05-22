"""Signal quality scoring (0-3).

SESSION 1 STUB. The real implementation (Session 5) calls the LLM with the
shared calibration set in core/calibration/signal_quality_examples.json. Until
then this returns a canned quality-3 score so the vertical slice runs.
"""
from __future__ import annotations

from dataclasses import dataclass

STUB = True


@dataclass
class QualityResult:
    signal_quality_score: int  # 0-3
    quality_reasoning: str


def score_signal(quoted_text: str, axis_relevance: list[str]) -> QualityResult:
    """STUB: assume a usable quality-3 signal. Replace in Session 5."""
    return QualityResult(
        signal_quality_score=3,
        quality_reasoning="stub: canned quality-3 score (Session 1 vertical slice)",
    )
