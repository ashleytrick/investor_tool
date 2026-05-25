"""Cold-outreach deliverability guardrails (Slice 9).

Pre-approval checks that surface as additional blockers in the
approval gate. Centralized here so the approval-blocker collector
in core/email/draft_routing.py + the manual approve_draft CLI + the
check_ready preflight all consult the same rule set.

Checks:

  - is_generic_or_role_email(email)
      info@ / hello@ / partners@ / team@ etc. Drafts to these
      addresses are usually noise; require an explicit override
      reason to approve.

  - check_recipient_duplicates(engine, partner_id, email)
      Same email address used by multiple partners is a sign of
      data-quality drift (typo, shared mailbox). Returns the list
      of partner_ids that share the recipient.

  - daily_approval_count(engine, today)
      How many drafts were approved today (UTC). Used by
      enforce_daily_approval_cap() to block approving > cap.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Final

from sqlalchemy import func, select

from core.db import draft_approvals, partners


# Local part of role / shared / catch-all mailboxes. Lowercased
# comparison against the part BEFORE the '@'. Whole-token match
# (not substring) so e.g. "info+vc@" still matches but "myinfo@"
# doesn't.
GENERIC_LOCAL_PARTS: Final[frozenset[str]] = frozenset({
    "info", "hello", "hi", "contact", "team", "partners",
    "investments", "deals", "deal-flow", "dealflow", "inbound",
    "intake", "admin", "support", "marketing", "press", "media",
    "office", "noreply", "no-reply", "do-not-reply",
})


# Valid `email_verification_status` values (text on partners). The
# operator sets these via a manual CLI (Slice 9 ships
# scripts/set_email_verification.py) or a future Apollo-import path.
VERIFICATION_UNKNOWN: Final[str] = "unknown"
VERIFICATION_VALID: Final[str] = "valid"
VERIFICATION_RISKY: Final[str] = "risky"
VERIFICATION_INVALID: Final[str] = "invalid"

ALL_VERIFICATION_STATUSES: Final[frozenset[str]] = frozenset({
    VERIFICATION_UNKNOWN, VERIFICATION_VALID,
    VERIFICATION_RISKY, VERIFICATION_INVALID,
})


# Statuses that don't block approval. `unknown` is permissive --
# legacy rows + non-verified-yet workspaces shouldn't require an
# override.
APPROVAL_OK_VERIFICATION_STATUSES: Final[frozenset[str]] = frozenset({
    VERIFICATION_UNKNOWN, VERIFICATION_VALID,
})


# Default daily approval cap. Operator can tune via
# company.yaml's `deliverability.daily_approval_cap`; the approval
# gate falls back here when unset.
DEFAULT_DAILY_APPROVAL_CAP: Final[int] = 25


def is_generic_or_role_email(email: str | None) -> bool:
    """True iff the email's local part is a generic/role mailbox.
    None / empty / no '@' return False (Slice 1's missing-email
    blocker covers those separately)."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].lower().strip()
    # Allow plus-tagged addresses (info+vc@) to match the base local.
    base = local.split("+", 1)[0]
    return base in GENERIC_LOCAL_PARTS


def check_recipient_duplicates(
    engine: Any, *, partner_id: str, email: str,
) -> list[str]:
    """Return the list of OTHER partner_ids that share this email.
    Empty list when the email is unique (or only used by partner_id
    itself). Case-insensitive comparison."""
    if not email:
        return []
    needle = email.strip().lower()
    with engine.begin() as conn:
        rows = conn.execute(
            select(partners.c.partner_id, partners.c.email).where(
                partners.c.partner_id != partner_id,
            )
        )
        return [
            r.partner_id for r in rows
            if (r.email or "").strip().lower() == needle
        ]


def daily_approval_count(
    engine: Any, *, today: date | None = None,
) -> int:
    """Count of approved_to_send events written today (UTC). Drives
    the daily cap check."""
    today = today or datetime.now(timezone.utc).date()
    # The draft_approvals table's `at` column is DateTime; we filter
    # by year-month-day in Python to avoid driver-specific date
    # casting. The numbers are small (cap << 100) so the SELECT is
    # cheap.
    with engine.begin() as conn:
        rows = conn.execute(
            select(draft_approvals.c.at).where(
                draft_approvals.c.event_type == "approved_to_send",
            )
        )
        count = 0
        for r in rows:
            ts = r.at
            if ts is None:
                continue
            if ts.date() == today:
                count += 1
        return count


def enforce_daily_approval_cap(
    engine: Any, *, cap: int = DEFAULT_DAILY_APPROVAL_CAP,
    today: date | None = None,
) -> tuple[bool, int]:
    """Return (blocked, count_today). True means the cap was reached
    and the caller should refuse the approval transition."""
    count = daily_approval_count(engine, today=today)
    return (count >= cap, count)
