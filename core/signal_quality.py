"""Signal quality scoring (0-3).

Live mode: LLM call against prompts/signal_quality.txt + the shared calibration
examples in core/calibration/signal_quality_examples.json. Stub mode (no API
key): a deterministic length/direction heuristic that produces sensible scores
for fixture inputs. Downstream gates use:
  - >= 2 may support Stage 6 scoring
  - >= 3 may open a signal_led email in Stage 7
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Optional

from core.llm.client import MODEL_BATCH, LLMClient
from schemas.signal_quality import SignalQuality

CALIB_PATH = (
    pathlib.Path(__file__).resolve().parent / "calibration" / "signal_quality_examples.json"
)
PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "prompts" / "signal_quality.txt"
)


@dataclass
class QualityResult:
    signal_quality_score: int
    quality_reasoning: str


def _load_calibration() -> str:
    """Render the shared calibration set as a stable text block for the prompt."""
    data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
    parts: list[str] = []
    for ex in data.get("examples", []):
        parts.append(
            f"  [{ex['score']}] \"{ex['quote']}\" — {ex['reasoning']}"
        )
    return "\n".join(parts)


def _heuristic(quoted_text: str, signal_direction: str, confidence: str) -> QualityResult:
    """Deterministic stub: length + direction + confidence gates the 0-3 score."""
    q = (quoted_text or "").strip()
    length = len(q)
    direction = (signal_direction or "").lower()
    confidence = (confidence or "").lower()

    if length < 20:
        return QualityResult(0, "stub heuristic: quote too short to carry signal")
    if direction not in ("positive", "negative"):
        return QualityResult(1, "stub heuristic: unclear direction")
    if length < 50 or confidence == "low":
        return QualityResult(1, "stub heuristic: short or low-confidence quote")
    if length < 80:
        return QualityResult(2,
                             "stub heuristic: medium-length quote with clear direction")
    return QualityResult(3,
                         "stub heuristic: long, specific quote with clear direction")


def score_signal(
    llm: Optional[LLMClient],
    *,
    quoted_text: str,
    axis_relevance: list[str],
    quote_date: Optional[str],
    source_url: str,
    signal_direction: str,
    confidence: str,
    company_description: str,
    company_name: str,
) -> QualityResult:
    """Return a 0-3 quality score with one-sentence reasoning."""
    if llm is None or llm.stub:
        return _heuristic(quoted_text, signal_direction, confidence)

    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{COMPANY_NAME}", company_name)
        .replace("{COMPANY_DESCRIPTION}", company_description)
        .replace("{QUOTED_TEXT}", quoted_text)
        .replace("{SOURCE_URL}", source_url)
        .replace("{AXIS_RELEVANCE}", ", ".join(axis_relevance))
        .replace("{QUOTE_DATE}", quote_date or "unknown")
        .replace("{CALIBRATION_EXAMPLES}", _load_calibration())
    )
    sq: SignalQuality = llm.complete_json(
        prompt=prompt,
        schema=SignalQuality,
        model=MODEL_BATCH,
    )
    return QualityResult(sq.signal_quality_score, sq.quality_reasoning)
