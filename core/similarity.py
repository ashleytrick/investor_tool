"""rapidfuzz wrappers, all returning normalized 0.0-1.0 scores.

Real implementation (rapidfuzz is trivial to wrap correctly). Used by the
Stage 7 batch similarity check.
"""
from __future__ import annotations

from rapidfuzz import fuzz


def token_set_similarity(a: str, b: str) -> float:
    """Order-insensitive similarity, normalized 0.0-1.0."""
    return fuzz.token_set_ratio(a or "", b or "") / 100.0


def ratio_similarity(a: str, b: str) -> float:
    """Plain ratio similarity, normalized 0.0-1.0."""
    return fuzz.ratio(a or "", b or "") / 100.0


def first_sentence(text: str) -> str:
    """Best-effort first sentence for first-sentence similarity checks."""
    text = (text or "").strip()
    for sep in (". ", "?\n", "\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text
