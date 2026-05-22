"""Pydantic schema for Stage 2 fund-enrichment LLM output."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, HttpUrl


class Partner(BaseModel):
    name: str
    title: Optional[str] = None
    bio_snippet: Optional[str] = None


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
