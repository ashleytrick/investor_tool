"""Pydantic schema for Stage 3 funding-announcement attribution LLM output.

Batch 10 tightening: required fields are min_length=1, round_size_usd has a
non-negative lower bound, announcement_date can't be in the future. A live
LLM returning {"company": ""} or {"round_size_usd": -100} or a tomorrow
date will trip ValidationError and retry instead of silently flowing junk
into deal_attributions.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AttributedPartner(BaseModel):
    name: str = Field(..., min_length=1)
    fund: str = Field(..., min_length=1)

    @field_validator("name", "fund", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v


class DealAttribution(BaseModel):
    company: str = Field(..., min_length=1)
    round_type: str = Field(..., min_length=1)
    # round_size_usd is the dollar amount. Negative is meaningless; the LLM
    # occasionally returns negative integers from misreading "down round" or
    # "wrote down". Reject so retry kicks in.
    round_size_usd: Optional[int] = Field(default=None, ge=0)
    lead_investor: Optional[str] = None
    all_investors: list[str] = []
    attributed_partners: list[AttributedPartner] = []
    sector_tags: list[str] = []
    announcement_date: Optional[date] = None

    @field_validator("company", "round_type", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("announcement_date")
    @classmethod
    def not_future_dated(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError(
                f"announcement_date {v} is in the future; LLM misparse"
            )
        return v
