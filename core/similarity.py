"""rapidfuzz wrappers, all returning normalized 0.0-1.0 scores.

Real implementation (rapidfuzz is trivial to wrap correctly). Used by the
Stage 7 batch similarity check.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

# First .!? followed by whitespace OR end-of-string, OR a bare paragraph
# break. Covers the cases the previous (". ", "?\n", "\n") tuple missed:
#   "Does that fit your thesis? We are raising..."   -> "?\\W"
#   "Wild quarter! Tendril is..."                    -> "!\\W"
#   "On the podcast.\n\nTendril is..."               -> ".\\n"
_SENT_END = re.compile(r"[.!?](?=\s|$)|\n")


def token_set_similarity(a: str, b: str) -> float:
    """Order-insensitive similarity, normalized 0.0-1.0."""
    return fuzz.token_set_ratio(a or "", b or "") / 100.0


def ratio_similarity(a: str, b: str) -> float:
    """Plain ratio similarity, normalized 0.0-1.0."""
    return fuzz.ratio(a or "", b or "") / 100.0


def first_sentence(text: str) -> str:
    """Best-effort first sentence for first-sentence similarity checks.

    Splits on .!? followed by whitespace OR end-of-string, OR a bare newline.
    The previous implementation only handled period+space, ?+newline, and
    bare newline -- so question-mark or exclamation openers compared full
    bodies and inflated first-sentence similarity.
    """
    text = (text or "").strip()
    m = _SENT_END.search(text)
    if m:
        # Match group is the punctuation char (length 1) or a newline.
        end = m.end()
        return text[:end].strip()
    return text
