"""Major-kill aggregation (Refactor item 7 / 13).

Stage 6 combines five distinct kill-signal sources into a single
`major_kill` boolean + a human-readable `kill_signal_summary`:

  1. round_fit disqualifiers (e.g. wrong stage, wrong check size)
  2. partner employment_status == "left_fund"
  3. fund.is_active is False AND round_fit didn't already award an
     "active_fund" component (defense-in-depth)
  4. fund.kill_signals from Stage 2 LLM extraction (Batch 16 #1110)
  5. partner.do_not_contact (Batch 26 #441/#684)

This module owns the OR'd-together logic + the summary-string
formatting so Stage 6 stays an orchestrator.
"""
from __future__ import annotations

from typing import Any, NamedTuple


class MajorKill(NamedTuple):
    """Stage 6's kill aggregation result.

    `present` -> major_kill_signal_present (boolean column).
    `summary` -> kill_signal_summary (human-readable column; "" when
                  present is False so the audit reads cleanly).
    """
    present: bool
    summary: str


def aggregate_major_kill(
    *,
    round_fit_result: Any,
    fund: Any,
    partner: Any,
) -> MajorKill:
    """Aggregate the five kill sources into MajorKill(present, summary).

    `round_fit_result` is a RoundFitResult-shaped object with:
      - disqualifier_present (bool)
      - triggered_disqualifiers (list[str])
      - components (dict[str, int]; expected key "active_fund")

    `fund` is a funds row with .is_active (bool) and .kill_signals
    (str | None) -- the latter is Stage 2's LLM-extracted free-text
    summary of any fund-level kill signals.

    `partner` is a partners row; getattr() is used for .do_not_contact
    and .do_not_contact_reason so older rows that pre-date the columns
    don't AttributeError.
    """
    # Source 4: fund.kill_signals (Stage 2 LLM extraction). Empty string
    # / None -> no kill. Non-empty -> kill, and the summary carries the
    # extracted text verbatim so the operator sees WHY.
    fund_kill_signals_str = (getattr(fund, "kill_signals", None) or "").strip()
    fund_has_kill = bool(fund_kill_signals_str)

    # Source 5: partner.do_not_contact. getattr() guards older rows.
    do_not_contact = bool(getattr(partner, "do_not_contact", False))

    present = (
        round_fit_result.disqualifier_present
        or (partner.employment_status == "left_fund")
        # Source 3: explicit defense-in-depth -- if the fund is
        # inactive AND round_fit didn't already award active_fund, the
        # partner should be killed even when round_fit produced no
        # disqualifier (e.g. older fixture data without is_active).
        or (
            not fund.is_active
            and round_fit_result.components.get("active_fund", 0) == 0
        )
        or fund_has_kill
        or do_not_contact
    )

    summary_parts: list[str] = []
    if round_fit_result.triggered_disqualifiers:
        summary_parts.extend(round_fit_result.triggered_disqualifiers)
    if fund_has_kill:
        summary_parts.append(f"fund kill_signals: {fund_kill_signals_str}")
    if do_not_contact:
        reason = getattr(partner, "do_not_contact_reason", None) or "-"
        summary_parts.append(f"do_not_contact: {reason}")

    summary = "; ".join(summary_parts) if present else ""
    return MajorKill(present=present, summary=summary)
