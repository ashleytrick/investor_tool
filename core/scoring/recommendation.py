"""Stage 6 criterion 1-9 recommendation gate (Refactor item 7 / 13).

Moved verbatim from scripts/06_score_candidates.py so the gate is
importable from anywhere (tests, future stages, future CLIs) without
loading the Stage 6 script as a module. Behavior is unchanged.

The criterion-10 finalization (strategy eligibility) still lives in
Stage 7; this module owns criteria 1-9 only.
"""
from __future__ import annotations

from datetime import date, datetime as _dt
from typing import Optional


# These constants are the brief's gate thresholds. Keeping them here
# (rather than in scripts/06) means doctor / status / other tools can
# read them without importing the Stage 6 script.
COMPOSITE_MIN = 6.5
ROUND_FIT_MIN = 6.0
LEAD_LIKELIHOOD_MIN = 5.0
COLD_REACHABILITY_MIN = 5.0
RECENT_EVIDENCE_WINDOW_DAYS = 540  # ~18 months


def evaluate_recommended(
    *,
    composite: float | None,
    round_fit_score: float,
    disqualifier_present: bool,
    lead_likelihood_score: float | None,
    distinct_source_types: int,
    q2_plus_signal_count: int,
    deal_attribution_count: int,
    most_recent_signal_date: date | None,
    employment_status: str | None,
    major_kill: bool,
    cold_reachability_score: float | None,
    warm_path_available: bool | None,
    today: date,
    # Batch 19 (#1101-#1103, #1125): suppress when an active outreach
    # cycle already exists for this partner. The outcomes table is the
    # source of truth; Stage 8 isn't (its preserve-on-outreach-started
    # logic depends on Attio state). Any of these latest-outcome
    # conditions means "don't re-recommend":
    #   - meeting_booked=True   (#1103)
    #   - reply_type=passed_*    (#1104) - they declined
    #   - reply_type=wrong_stage (#1105) - they're not the right partner
    #   - outreach_status in {sent, replied, meeting_booked} and the
    #     last outcome is within `latest_outcome_window_days`
    #     (default 30) -- prevents re-outreach within a month
    latest_outcome: dict | None = None,
    latest_outcome_window_days: int = 30,
) -> tuple[bool, str]:
    """Return (recommended, reasoning).

    `reasoning` is a human-readable explanation: a success sentence when
    `recommended` is True, or `"Not recommended: <fail1>; <fail2>; ..."`
    when False. Stage 6 persists this string into
    partner_score_summaries.recommendation_reasoning verbatim so the
    operator can audit per-partner without rerunning anything.
    """
    fails: list[str] = []
    if latest_outcome:
        # Hard suppressions: terminal states regardless of recency.
        if bool(latest_outcome.get("meeting_booked")):
            fails.append(
                "meeting already booked (outcomes row present); "
                "do not re-recommend"
            )
        reply_type = (latest_outcome.get("reply_type") or "")
        if reply_type.startswith("passed"):
            fails.append(
                f"partner replied reply_type={reply_type!r} (passed); "
                "do not re-outreach"
            )
        if reply_type == "wrong_stage":
            fails.append(
                "partner replied reply_type='wrong_stage'; do not re-outreach"
            )
        # Recent active outreach (sent / replied / in flight) -> wait.
        status = latest_outcome.get("outreach_status")
        ts = latest_outcome.get("synced_from_attio_at")
        if status in ("sent", "replied", "meeting_booked") and ts is not None:
            try:
                # SQLite returns naive datetimes; compare consistently.
                age_days = (_dt.utcnow() - ts).days
            except (AttributeError, TypeError):
                age_days = None
            if age_days is not None and age_days < latest_outcome_window_days:
                fails.append(
                    f"active outreach: outreach_status={status!r} {age_days}d "
                    f"ago (< {latest_outcome_window_days}d window); "
                    f"do not re-recommend"
                )
    if composite is None or composite < COMPOSITE_MIN:
        fails.append(f"composite_fit_score ({composite}) < {COMPOSITE_MIN}")
    if round_fit_score < ROUND_FIT_MIN or disqualifier_present:
        fails.append(
            f"round_fit_score ({round_fit_score:.1f}) < {ROUND_FIT_MIN} "
            f"or disqualifier present"
        )
    # Batch 36 (#22): same semantics as Batch 35's cold_reachability fix.
    # None used to permit recommendation; now it blocks with an explicit
    # reason so the operator knows Stage 6's lead_likelihood input is
    # missing for this partner.
    if lead_likelihood_score is None:
        fails.append(
            "lead_likelihood_score is unknown (Stage 6 had no inputs to "
            "compute it); the partner needs at least one attributed deal "
            "before recommendation"
        )
    elif lead_likelihood_score < LEAD_LIKELIHOOD_MIN:
        fails.append(
            f"lead_likelihood_score ({lead_likelihood_score:.1f}) "
            f"< {LEAD_LIKELIHOOD_MIN}"
        )
    # criterion 4: 2 distinct evidence sources at quality>=2
    crit4_ok = (
        distinct_source_types >= 2
        or (q2_plus_signal_count >= 1 and deal_attribution_count >= 1)
    )
    if not crit4_ok:
        fails.append(
            "fewer than 2 distinct verified quality>=2 evidence sources "
            "(need 2 distinct source_types or 1 thesis + 1 deal pattern)"
        )
    # within_days() = past-and-bounded; future-dated quotes don't sneak past.
    from core.dates import within_days as _within
    if not _within(most_recent_signal_date, RECENT_EVIDENCE_WINDOW_DAYS, today):
        fails.append("no verified quality>=2 evidence within last 18 months")
    if employment_status not in ("verified_current", "likely_current"):
        fails.append(f"employment_status={employment_status!r} not current")
    if major_kill:
        fails.append("major kill signal present")
    # Batch 35: unknown reachability (None) used to permit recommendation
    # because the check was `is not None and < 5.0`. That left partners
    # with zero reachability evidence treated as "good enough" while
    # partners with a measured low score were blocked -- the opposite of
    # the safety-conscious default. Treat None as blocking; the operator
    # can promote via --force-rescore after Stage 4 produces a partial.
    if cold_reachability_score is None:
        fails.append(
            "cold_reachability_score is unknown (Stage 4 produced no "
            "partial); re-run Stage 4 to score reachability before "
            "recommending"
        )
    elif cold_reachability_score < COLD_REACHABILITY_MIN:
        fails.append(
            f"cold_reachability_score ({cold_reachability_score:.1f}) "
            f"< {COLD_REACHABILITY_MIN}"
        )
    if warm_path_available is True:
        fails.append("warm_path_available=TRUE -- prefer warm route")
    ok = not fails
    reasoning = (
        "All Stage 6 criteria met; Stage 7 finalizes (strategy eligibility)."
        if ok
        else "Not recommended: " + "; ".join(fails)
    )
    return ok, reasoning
