"""Pydantic schema for Stage 2 fund-enrichment LLM output.

Batch 10 adds a normalized `stated_stage_focus` accepting the canonical set
the rest of the pipeline understands. Free-form values are coerced to the
nearest canonical match; anything genuinely unknown raises so retry can
fix it. Stage 6 round_fit relies on these labels for stage_match scoring;
arbitrary strings would silently fail to match anything.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator

# Canonical stage labels used by Stage 6 round_fit. Stage 6's STAGE_ACCEPT
# keys against these exact strings (after lowercasing).
CANONICAL_STAGES = {
    "pre-seed", "seed", "series a", "series b", "series c",
    "growth", "multi-stage", "late-stage",
}


def _canonicalize_stage(raw: str) -> Optional[str]:
    """Map common LLM variants to a canonical stage label. Returns None
    if no reasonable match (caller raises)."""
    s = raw.strip().lower()
    s = re.sub(r"[\s_]+", " ", s)
    s = s.replace("series-", "series ")
    # Direct canonical match
    if s in CANONICAL_STAGES:
        return s
    # Common variants
    aliases = {
        "preseed": "pre-seed",
        "pre seed": "pre-seed",
        "series-a": "series a",
        "series-b": "series b",
        "series-c": "series c",
        "a": "series a",
        "b": "series b",
        "c": "series c",
        "multistage": "multi-stage",
        "multi stage": "multi-stage",
        "growth-stage": "growth",
        "late stage": "late-stage",
        "late": "late-stage",
    }
    return aliases.get(s)


class Partner(BaseModel):
    name: str = Field(..., min_length=1)
    title: Optional[str] = None
    bio_snippet: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        # Avoid generating partner_ids from " " or whitespace-only LLM output,
        # which would collide with other empty-name partners at the same fund.
        if isinstance(v, str):
            return v.strip()
        return v


class FundEnrichment(BaseModel):
    thesis_summary: Optional[str] = None
    stated_sectors: list[str] = []
    stated_stage_focus: Optional[str] = None
    check_size_range: Optional[str] = None
    portfolio_companies: list[str] = []
    current_partners: list[Partner] = []
    recent_focus_signals: Optional[str] = None
    explicit_kill_signals: list[str] = []
    source_urls_used: list[HttpUrl] = []

    @field_validator("stated_stage_focus")
    @classmethod
    def canonicalize_stage(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        canonical = _canonicalize_stage(v)
        if canonical is None:
            raise ValueError(
                f"stated_stage_focus {v!r} not in canonical set "
                f"{sorted(CANONICAL_STAGES)}; LLM should map to one of these"
            )
        return canonical
