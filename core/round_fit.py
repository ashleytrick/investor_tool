"""Deterministic round_fit calculation.

Computed entirely from observable facts. The LLM never produces the score;
round_fit_reasoning is templated from the components dict so the operator can
see exactly which signals drove the number.

Components per the brief:
  stage_match            0 or 3
  check_size_match       0 or 2
  active_fund            0 or 2
  recent_relevant_deals  0 to 2
  partner_decision_power 0 or 1

  round_fit_score = sum of above (max 10)
  disqualifier_present caps the final score at 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ----- stage matching -----

# The seed/A target round matches itself plus multi-stage funds.
STAGE_ACCEPT: dict[str, set[str]] = {
    "pre-seed": {"pre-seed", "multi-stage"},
    "seed": {"seed", "multi-stage"},
    "series a": {"series a", "multi-stage"},
}


def _norm_stage(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.replace("series-a", "series a")


def stage_matches(raise_round: str, fund_stage: Optional[str]) -> bool:
    accept = STAGE_ACCEPT.get(_norm_stage(raise_round), set())
    return _norm_stage(fund_stage) in accept if accept else False


# ----- check size parsing + overlap -----

_SIZE_RE = re.compile(r"\$?\s*([\d.]+)\s*([KMB])?", re.IGNORECASE)
_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_one_amount(token: str) -> Optional[int]:
    m = _SIZE_RE.match(token.strip())
    if not m:
        return None
    val = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    if suffix:
        val *= _MULT[suffix]
    return int(val)


def parse_check_size(raw: Optional[str]) -> Optional[tuple[int, int]]:
    """'$500K-$2M' -> (500000, 2000000). Returns None if not parseable."""
    if not raw:
        return None
    parts = re.split(r"\s*[-–to]+\s*", raw.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    low = _parse_one_amount(parts[0])
    high = _parse_one_amount(parts[1])
    if low is None or high is None:
        return None
    return min(low, high), max(low, high)


def ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


# ----- partner decision power -----

_GP_PAT = re.compile(
    r"\b(general\s*partner|managing\s*partner|founding\s*partner|gp|md)\b",
    re.IGNORECASE,
)
_ASSOC_PAT = re.compile(r"\b(associate|analyst|intern)\b", re.IGNORECASE)


def partner_decision_power(title: Optional[str]) -> int:
    t = title or ""
    if _ASSOC_PAT.search(t):
        return 0
    # GP/MD or generic "Partner"/"Principal" -> 1.
    if _GP_PAT.search(t) or re.search(r"\b(partner|principal)\b", t, re.IGNORECASE):
        return 1
    return 0


# ----- recent_relevant_deals -----

def recent_relevant_deals(
    fund_deals_18mo: list[dict],
    target_sectors: list[str],
) -> int:
    """Count fund's last-18mo lead deals whose sector_tags hit target_sectors."""
    targets = {t.strip().lower() for t in (target_sectors or []) if t.strip()}
    if not targets:
        return 0
    hits = 0
    for d in fund_deals_18mo:
        tags = {t.strip().lower() for t in (d.get("sector_tags") or [])}
        if tags & targets:
            hits += 1
    return min(2, hits)


# ----- disqualifier evaluation -----

def _disq_growth_only(fund_stage_norm: str) -> bool:
    return fund_stage_norm in {"growth", "growth-stage", "late-stage"}


def _disq_stage_mismatch(raise_round_norm: str, fund_stage_norm: str) -> bool:
    """Pre-seed-only when raising seed; seed-only when raising A."""
    if raise_round_norm == "seed" and fund_stage_norm == "pre-seed":
        return True
    if raise_round_norm == "series a" and fund_stage_norm == "seed":
        return True
    if raise_round_norm == "seed" and fund_stage_norm in {"series a", "series b", "growth"}:
        return True
    return False


def evaluate_disqualifiers(
    disqualifier_strs: list[str],
    fund_row: dict,
    raise_round: str,
    check_size_match: int,
    has_led_recently: bool,
) -> list[str]:
    """Return the list of triggered disqualifier strings."""
    fund_stage_n = _norm_stage(fund_row.get("stated_stage_focus"))
    raise_n = _norm_stage(raise_round)
    triggered: list[str] = []
    for raw in disqualifier_strs:
        d = raw.lower()
        if "growth-only" in d and _disq_growth_only(fund_stage_n):
            triggered.append(raw)
        elif ("pre-seed-only" in d or "seed-only" in d) and _disq_stage_mismatch(
            raise_n, fund_stage_n
        ):
            triggered.append(raw)
        elif "follow-on" in d and "never leads" in d and not has_led_recently:
            triggered.append(raw)
        elif "not currently deploying" in d and not fund_row.get("is_active"):
            triggered.append(raw)
        elif "check size constraint" in d and check_size_match == 0:
            triggered.append(raw)
    return triggered


# ----- main -----

@dataclass
class RoundFitResult:
    round_fit_score: float
    round_fit_reasoning: str
    disqualifier_present: bool
    triggered_disqualifiers: list[str] = field(default_factory=list)
    components: dict = field(default_factory=dict)


def compute_round_fit(
    fund_row: dict,
    partner_row: dict,
    fund_deals_18mo: list[dict],
    fund_has_led_recently: bool,
    company_cfg: dict,
) -> RoundFitResult:
    raise_round = company_cfg["raise_context"]["round"]
    target_lo = int(company_cfg["company"]["target_check_size_usd"]["min"])
    target_hi = int(company_cfg["company"]["target_check_size_usd"]["max"])
    target_sectors = company_cfg["company"].get("target_sectors", []) or []
    disq_strs = (company_cfg.get("round_fit") or {}).get("disqualifiers", []) or []

    sm = 3 if stage_matches(raise_round, fund_row.get("stated_stage_focus")) else 0

    fund_range = parse_check_size(fund_row.get("check_size_range"))
    csm = 2 if (fund_range and ranges_overlap(fund_range, (target_lo, target_hi))) else 0

    af = 2 if fund_row.get("is_active") else 0
    rrd = recent_relevant_deals(fund_deals_18mo, target_sectors)
    pdp = partner_decision_power(partner_row.get("title"))

    components = {
        "stage_match": sm,
        "check_size_match": csm,
        "active_fund": af,
        "recent_relevant_deals": rrd,
        "partner_decision_power": pdp,
    }
    raw_score = sm + csm + af + rrd + pdp

    triggered = evaluate_disqualifiers(
        disq_strs, fund_row, raise_round, csm, fund_has_led_recently
    )
    disqualifier_present = bool(triggered)
    final_score = min(2.0, float(raw_score)) if disqualifier_present else float(raw_score)

    # Templated reasoning so the operator can see what drove the number.
    parts: list[str] = []
    parts.append(
        f"stage={'match' if sm else 'mismatch'}({sm}/3), "
        f"check_size={'overlap' if csm else 'no overlap'}({csm}/2), "
        f"active={'yes' if af else 'no'}({af}/2), "
        f"relevant_deals={rrd}/2, "
        f"decision_power={pdp}/1"
    )
    if disqualifier_present:
        parts.append(f"disqualifier(s): {', '.join(triggered)} -> capped at 2")
    reasoning = "; ".join(parts)

    return RoundFitResult(
        round_fit_score=final_score,
        round_fit_reasoning=reasoning,
        disqualifier_present=disqualifier_present,
        triggered_disqualifiers=triggered,
        components=components,
    )


def is_within_months(d: Optional[date], months: int, today: Optional[date] = None) -> bool:
    """Within `months` AND not in the future (date.dates helper guards both)."""
    from core.dates import within_days
    return within_days(d, int(months * 30.5), today)
