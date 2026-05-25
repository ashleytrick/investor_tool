"""Cold-outreach relationship state + suppression rules (Slice 7).

A partner's relationship_status records what we know about prior
interaction. The approval gate + Stage 6 recommendation gate read
this state and refuse outreach when the operator's existing
conversation / explicit pass / DNC says so.

Values (single source of truth -- mirrors core.db.partners.relationship_status):
  none                 -- default; no prior interaction
  known                -- on radar, no outreach yet
  contacted            -- sent outreach (Stage 7 + send queue side)
  active_conversation  -- replied; conversation still open
  passed               -- declined (within PASSED_COOLDOWN_DAYS = suppressed)
  invested             -- terminal positive (suppressed permanently)
  do_not_contact       -- terminal negative (always suppressed)

Suppression rules
-----------------
suppress_outreach(state, last_contacted_at, last_reply_at, now) -> tuple[bool, str|None]

Returns (is_suppressed, reason_or_None). The reason is operator-
visible: surfaces in the review CSV + check_ready output.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final


STATE_NONE: Final[str] = "none"
STATE_KNOWN: Final[str] = "known"
STATE_CONTACTED: Final[str] = "contacted"
STATE_ACTIVE_CONVERSATION: Final[str] = "active_conversation"
STATE_PASSED: Final[str] = "passed"
STATE_INVESTED: Final[str] = "invested"
STATE_DO_NOT_CONTACT: Final[str] = "do_not_contact"

ALL_STATES: Final[frozenset[str]] = frozenset({
    STATE_NONE, STATE_KNOWN, STATE_CONTACTED, STATE_ACTIVE_CONVERSATION,
    STATE_PASSED, STATE_INVESTED, STATE_DO_NOT_CONTACT,
})


# How long after a `passed` reply we suppress re-outreach. Defaults
# to 180 days; configurable per-workspace via company.yaml's
# `relationship.passed_cooldown_days` once Slice 14 (mode policy)
# lands.
PASSED_COOLDOWN_DAYS: Final[int] = 180

# How recently we'd consider a partner "contacted" enough to skip
# duplicate outreach. Same configurability future.
CONTACTED_COOLDOWN_DAYS: Final[int] = 30


@dataclass(frozen=True)
class Suppression:
    suppressed: bool
    reason: str | None

    @classmethod
    def allow(cls) -> "Suppression":
        return cls(suppressed=False, reason=None)


def _days_ago(ts: datetime | None, now: datetime) -> int | None:
    """Days between `ts` and `now`. None when ts is None or ts > now
    (a future timestamp shouldn't be treated as 'recent')."""
    if ts is None:
        return None
    # Normalize aware/naive: drop tz for comparison.
    a = ts.replace(tzinfo=None) if ts.tzinfo else ts
    b = now.replace(tzinfo=None) if now.tzinfo else now
    if a > b:
        return None
    return (b - a).days


def suppress_outreach(
    *,
    relationship_status: str | None,
    last_contacted_at: datetime | None,
    last_reply_at: datetime | None,
    do_not_contact: bool,
    now: datetime | None = None,
    passed_cooldown_days: int = PASSED_COOLDOWN_DAYS,
    contacted_cooldown_days: int = CONTACTED_COOLDOWN_DAYS,
) -> Suppression:
    """Single source of truth for "should we suppress cold outreach?".

    Returns Suppression(suppressed, reason). The approval gate + the
    Stage 6 recommendation gate + check_ready all call this. Adding
    a new suppression rule means editing exactly this function.
    """
    now = now or datetime.now(timezone.utc)

    # Always-suppress states.
    if do_not_contact:
        return Suppression(True, "do_not_contact is set on the partner")
    if relationship_status == STATE_DO_NOT_CONTACT:
        return Suppression(True, "relationship_status=do_not_contact")
    if relationship_status == STATE_INVESTED:
        return Suppression(True, "relationship_status=invested (closed deal)")
    if relationship_status == STATE_ACTIVE_CONVERSATION:
        return Suppression(
            True,
            "active_conversation -- cold outreach would interrupt the "
            "existing conversation",
        )

    # Time-bounded suppression.
    days_since_reply = _days_ago(last_reply_at, now)
    if relationship_status == STATE_PASSED:
        if days_since_reply is None:
            # `passed` recorded without a reply timestamp -- be
            # conservative and suppress.
            return Suppression(
                True, "relationship_status=passed (no reply timestamp; "
                      "suppressed by default)",
            )
        if days_since_reply < passed_cooldown_days:
            return Suppression(
                True,
                f"partner passed {days_since_reply}d ago "
                f"(< {passed_cooldown_days}d cooldown)",
            )

    days_since_contact = _days_ago(last_contacted_at, now)
    if relationship_status == STATE_CONTACTED and days_since_contact is not None:
        if days_since_contact < contacted_cooldown_days:
            return Suppression(
                True,
                f"contacted {days_since_contact}d ago "
                f"(< {contacted_cooldown_days}d cooldown)",
            )

    return Suppression.allow()


# ----- outcome -> relationship hydration -----


# Map outcome event reply_type values to relationship states.
# Used by core.outcomes.persistence after each outcome event lands.
_REPLY_TYPE_TO_STATE: Final[dict[str, str]] = {
    "passed_not_a_fit": STATE_PASSED,
    "passed_too_early": STATE_PASSED,
    "passed_no_fit": STATE_PASSED,
    "passed_wrong_stage": STATE_PASSED,
    "passed_other": STATE_PASSED,
    "interested": STATE_ACTIVE_CONVERSATION,
    "meeting_requested": STATE_ACTIVE_CONVERSATION,
    "follow_up_requested": STATE_ACTIVE_CONVERSATION,
    "invested": STATE_INVESTED,
    # Anything else stays at whatever it already is.
}


def state_from_outcome_event(
    *,
    outreach_status: str | None,
    reply_type: str | None,
    meeting_booked: bool,
    meeting_outcome: str | None,
) -> str | None:
    """Translate an outcome event into the relationship state it
    implies. Returns None when the event doesn't imply any specific
    state (caller leaves the existing relationship_status alone).
    """
    # Reply types are the strongest signal.
    if reply_type:
        mapped = _REPLY_TYPE_TO_STATE.get(reply_type)
        if mapped:
            return mapped
    if meeting_booked or meeting_outcome:
        # A booked meeting implies an active conversation regardless
        # of how it ends (the post-meeting outcome flips it later).
        return STATE_ACTIVE_CONVERSATION
    if outreach_status in ("sent", "replied", "meeting_booked"):
        return STATE_CONTACTED
    return None
