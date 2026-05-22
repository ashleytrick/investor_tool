"""Pydantic schema for Stage 3 funding-announcement attribution LLM output."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class AttributedPartner(BaseModel):
    name: str
    fund: str


class DealAttribution(BaseModel):
    company: str
    round_type: str
    round_size_usd: Optional[int] = None
    lead_investor: Optional[str] = None
    all_investors: list[str] = []
    attributed_partners: list[AttributedPartner] = []
    sector_tags: list[str] = []
    announcement_date: Optional[date] = None
