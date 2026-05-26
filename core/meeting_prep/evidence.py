"""Shared evidence loader for the meeting-prep builders.

Both objection_map and framing_brief need the same per-partner
context: the verified, quality>=2 signals + the partner-led deals +
the fund row + the partner row. Centralized here so the two builders
hash the same set (same cache key for the same evidence) and pass
identical JSON to the LLM.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine

from core.db import (
    deal_attributions,
    funds,
    partner_score_summaries,
    partners,
    signals,
)

# The signal quality floor for ANYTHING this module emits to the
# operator. Mirrors core/signal_quality.py's downstream gate.
QUALITY_FLOOR = 2

# Minimum number of quality>=2 signals required before we'll let the
# builder produce partner-specific output. Below this, both builders
# set insufficient_evidence=True and skip the LLM call entirely.
MIN_SIGNALS_FOR_PARTNER_SPECIFIC = 2


@dataclass
class PartnerEvidence:
    partner_id: str
    partner_row: Any
    fund_row: Any | None
    summary_row: Any | None
    verified_signals: list[dict]   # ready for JSON serialization
    partner_deals: list[dict]
    quality_signal_ids: list[int]  # subset with quality>=2

    @property
    def has_enough_signals(self) -> bool:
        return len(self.quality_signal_ids) >= MIN_SIGNALS_FOR_PARTNER_SPECIFIC


def load_evidence(engine: Engine, partner_id: str) -> PartnerEvidence | None:
    """Build the evidence bundle. Returns None if the partner_id
    doesn't exist -- callers translate that to a 'not found' error
    so the cache layer never gets a phantom key."""
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(partners).where(partners.c.partner_id == partner_id)
        ).first()
        if partner_row is None:
            return None
        fund_row = conn.execute(
            select(funds).where(funds.c.fund_id == partner_row.fund_id)
        ).first() if partner_row.fund_id else None
        summary_row = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == partner_id
            )
        ).first()
        # Verified signals only. Quality floor applied in the dict
        # below; we still surface low-quality signals to the LLM for
        # context but they don't count toward the "enough signals"
        # gate.
        sig_rows = list(conn.execute(
            select(signals).where(
                signals.c.partner_id == partner_id,
                signals.c.verified.is_(True),
            ).order_by(desc(signals.c.signal_quality_score),
                       desc(signals.c.quote_date))
        ))
        deal_rows = list(conn.execute(
            select(deal_attributions).where(
                deal_attributions.c.attributed_partner_id == partner_id
            ).order_by(desc(deal_attributions.c.announcement_date)).limit(10)
        ))

    verified_signals: list[dict] = []
    quality_ids: list[int] = []
    for s in sig_rows:
        try:
            axes = json.loads(s.axis_relevance or "[]")
        except json.JSONDecodeError:
            axes = []
        verified_signals.append({
            "signal_id": int(s.signal_id),
            "source_type": s.source_type,
            "source_url": s.source_url,
            "quote_date": str(s.quote_date) if s.quote_date else None,
            "axis_relevance": axes,
            "signal_direction": s.signal_direction,
            "signal_quality_score": s.signal_quality_score,
            "quote": s.quoted_text,
        })
        if (s.signal_quality_score or 0) >= QUALITY_FLOOR:
            quality_ids.append(int(s.signal_id))

    partner_deals = [
        {
            "company": d.company,
            "round_type": d.round_type,
            "round_size_usd": d.round_size_usd,
            "announcement_date": (
                str(d.announcement_date) if d.announcement_date else None
            ),
        }
        for d in deal_rows
    ]

    return PartnerEvidence(
        partner_id=partner_id,
        partner_row=partner_row,
        fund_row=fund_row,
        summary_row=summary_row,
        verified_signals=verified_signals,
        partner_deals=partner_deals,
        quality_signal_ids=quality_ids,
    )
