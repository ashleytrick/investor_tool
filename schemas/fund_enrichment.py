"""Pydantic schema for Stage 2 fund-enrichment LLM output."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


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
