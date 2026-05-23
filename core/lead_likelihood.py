"""Mostly-deterministic lead_likelihood calculation.

Score derived entirely from observable facts (named-as-lead counts, title
seniority, attribution patterns). The LLM is never invoked here; reasoning
text is rendered from the component breakdown. lead_likelihood_signals is
returned as a JSON list of supporting evidence rows.

Score = clamp(
    named_as_lead_count * 2
  + recent_board_seats         # board-seat data not yet ingested; 0 for now
  + solo_check_pattern         # 0 or 2: any deal where partner is sole named lead
  + title_seniority            # 0/1/2
  + follow_on_only_flag        # -3 if every recent attributed deal is a follow-on
  , 0, 10)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

LEAD_WINDOW_DAYS = 730  # 24 months


def _title_seniority(title: Optional[str]) -> int:
    t = (title or "").lower()
    if any(k in t for k in ("general partner", "managing partner", "founding partner", " md", "gp")):
        return 2
    if "partner" in t or "principal" in t:
        return 1
    return 0


def _is_recent(d: Optional[date], today: Optional[date] = None) -> bool:
    if d is None:
        return False
    today = today or date.today()
    return (today - d) <= timedelta(days=LEAD_WINDOW_DAYS)


@dataclass
class LeadLikelihoodResult:
    lead_likelihood_score: float
    lead_likelihood_signals: str  # JSON list of evidence dicts
    components: dict


def compute_lead_likelihood(
    partner_row: dict,
    attributed_deals: list[dict],
    today: Optional[date] = None,
) -> LeadLikelihoodResult:
    """`attributed_deals`: deal_attributions where attributed_partner_id=partner."""
    recent = [d for d in attributed_deals if _is_recent(d.get("announcement_date"), today)]

    named_as_lead_count = len(recent)
    title_score = _title_seniority(partner_row.get("title"))
    solo_check = 2 if named_as_lead_count >= 1 else 0
    board_seats = 0  # ingestion of board-seat data lands later
    follow_on_only = 0  # follow-on classification not yet ingested

    raw = named_as_lead_count * 2 + board_seats + solo_check + title_score + follow_on_only
    score = max(0.0, min(10.0, float(raw)))

    evidence = [
        {
            "evidence": (
                f"named lead in {d.get('company','?')} "
                f"({d.get('round_type','?')}"
                + (f" ${d.get('round_size_usd'):,}" if d.get("round_size_usd") else "")
                + ")"
            ),
            "source_url": d.get("source_url"),
            "date": d.get("announcement_date").isoformat() if d.get("announcement_date") else None,
        }
        for d in recent
    ]
    if not evidence:
        evidence.append({
            "evidence": (
                f"no partner-attributed lead deals in last {LEAD_WINDOW_DAYS // 30} months; "
                f"title seniority contributes {title_score}/2"
            ),
            "source_url": None,
            "date": None,
        })

    components = {
        "named_as_lead_count": named_as_lead_count,
        "title_seniority": title_score,
        "solo_check_pattern": solo_check,
        "recent_board_seats": board_seats,
        "follow_on_only_flag": follow_on_only,
    }
    return LeadLikelihoodResult(
        lead_likelihood_score=score,
        lead_likelihood_signals=json.dumps(evidence),
        components=components,
    )
