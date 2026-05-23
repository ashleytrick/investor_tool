"""Pydantic schema for reply-classification LLM output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ReplyType = Literal[
    "no_response",
    "booked",
    "asked_for_deck",
    "passed_too_early",
    "passed_category",
    "wrong_stage",
    "asked_for_more_info",
    "referred_to_colleague",
    "warm_intro_requested",
]


class ReplyClassification(BaseModel):
    reply_type: ReplyType
    confidence: Literal["low", "medium", "high"]
    reasoning: str
    meeting_booked: bool = False
