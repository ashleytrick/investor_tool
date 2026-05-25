"""Stage 7 strategy eligibility (Refactor item 7 / 14).

Pure functions extracted from scripts/07_generate_emails.py so they can
be unit-tested without spinning up a workspace. Behavior unchanged.

The brief defines 6 outreach strategies for cold partner emails:
  - signal_led        (anchor on a verified partner quote)
  - portfolio_led     (anchor on adjacent portfolio company)
  - round_pattern_led (anchor on partner's recent led deals)
  - market_shift_led  (anchor on timing / policy-driven window)
  - contrarian_thesis_led (anchor on a strong-belief axis)
  - traction_led      (anchor on company metrics)

Each strategy gets a 0-3 eligibility score from compute_eligibility().
pick_strategies() returns up to two strategies at >=2, breaking ties
via STRATEGY_TIE_BREAK so the same evidence shape always produces the
same picks.
"""
from __future__ import annotations


# Keywords in a partner's quoted signal that indicate they care about
# metrics / traction / customer evidence. The brief: traction_led
# requires "strong company traction AND a metrics-oriented partner
# signal", not just strong company traction alone.
METRICS_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "metric", "metrics", "arr", "retention", "nrr", "growth", "growing",
    "customers", "revenue", "churn", "conversion", "users", "scale",
    "burn", "design partner", "design partners", "sign-up", "sign-ups",
    "sales", "pipeline",
)


# Axis name/description tokens that mark a "timing / market-shift"
# belief axis. Previously Stage 7 hardcoded axis_4 (the fixture's
# timing axis) to enable market_shift_led -- which broke for any
# workspace that doesn't put the timing-driven axis last. Now we
# resolve it by inspecting axes.yaml so eligibility is workspace-
# portable.
MARKET_SHIFT_AXIS_TOKENS: tuple[str, ...] = (
    "timing", "market shift", "market-shift", "window", "policy",
    "forced buy", "tailwind", "regulatory",
)


# Tie-break order when two strategies score equally: strongest evidence
# shape first. signal_led/portfolio_led/round_pattern_led are concrete;
# market_shift and traction lean general; contrarian_thesis_led is
# last because it leans on rhetorical risk.
STRATEGY_TIE_BREAK: tuple[str, ...] = (
    "signal_led",
    "portfolio_led",
    "round_pattern_led",
    "market_shift_led",
    "traction_led",
    "contrarian_thesis_led",
)


def has_metrics_oriented_signal(p_signals: list[dict]) -> bool:
    """True iff at least one verified signal mentions metrics vocabulary."""
    for s in p_signals:
        text = (s.get("quote") or "").lower()
        if any(kw in text for kw in METRICS_SIGNAL_KEYWORDS):
            return True
    return False


def market_shift_axis_ids(axes_cfg: dict) -> set[str]:
    """Return axis IDs whose name or description signals a timing /
    market-shift belief. Falls back to {} if no axis matches -- callers
    treat that as market_shift_led ineligible, which is the safe
    default."""
    out: set[str] = set()
    for ax in (axes_cfg or {}).get("axes", []) or []:
        blob = " ".join((
            (ax.get("name") or ""),
            (ax.get("description") or ""),
        )).lower()
        if any(tok in blob for tok in MARKET_SHIFT_AXIS_TOKENS):
            out.add(ax.get("id"))
    return {aid for aid in out if aid}


def has_company_traction(company_cfg: dict) -> bool:
    """True iff the company config carries any traction proof (headline
    metric or any secondary metrics)."""
    c = (company_cfg.get("company") or {}).get("current_traction") or {}
    return bool(c.get("headline_metric")) or bool(c.get("secondary_metrics"))


def compute_eligibility(
    has_q3: bool,
    has_q2: bool,
    fund_adjacent: bool,
    partner_led_in_target: bool,
    market_window_match: bool,
    company_traction_proof: bool,
) -> dict[str, int]:
    """0-3 score per strategy. Only >=2 may be used by the email
    picker.

    `company_traction_proof` is caller-computed as
    has_company_traction(...) AND has_metrics_oriented_signal(...). It
    must NOT be hardcoded True per the brief's finding #11.
    """
    return {
        "signal_led": 3 if has_q3 else (2 if has_q2 else 0),
        "portfolio_led": 3 if fund_adjacent else 0,
        "round_pattern_led": 3 if partner_led_in_target else 0,
        "market_shift_led": 2 if market_window_match else 0,
        "contrarian_thesis_led": 2 if has_q3 else 0,
        "traction_led": 2 if company_traction_proof else 0,
    }


def pick_strategies(elig: dict[str, int]) -> list[str]:
    """Return up to two eligible strategies, highest score first then
    tie-break order."""
    eligible = sorted(
        [(s, score) for s, score in elig.items() if score >= 2],
        key=lambda x: (-x[1], STRATEGY_TIE_BREAK.index(x[0])),
    )
    return [s for s, _ in eligible[:2]]
