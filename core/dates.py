"""Date-distance helpers that refuse to count future dates as "recent".

Multiple stages (lead_likelihood, round_fit, send_now_priority's recency
bonus, Stage 3 fund activity, Stage 6 cold_reachability) all compute
"is this date within N days of today" using `(today - d) <= timedelta(N)`.
That naively passes when `d` is in the future, because the timedelta is
negative and still <= the bound. Bad parsed dates -> inflated scores.

These helpers clamp the lower bound to 0, so future dates are treated as
"unknown" rather than "yesterday".
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def days_since(d: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Return the integer day delta `today - d`, but ONLY when d is in the
    past (or today). For None or future dates, returns None so any subsequent
    `<=` comparison short-circuits to False.
    """
    if d is None:
        return None
    today = today or date.today()
    delta = (today - d).days
    return delta if delta >= 0 else None


def within_days(
    d: Optional[date], days: int, today: Optional[date] = None
) -> bool:
    """True iff d is in [today - days, today]. False for None or future d."""
    ds = days_since(d, today)
    return ds is not None and ds <= days


def cutoff_date(days: int, today: Optional[date] = None) -> date:
    """today - days. Use with `>= cutoff_date(...) AND <= today` predicates
    so future dates don't sneak past a one-sided cutoff."""
    today = today or date.today()
    return today - timedelta(days=days)
