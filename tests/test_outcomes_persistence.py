"""Unit tests for core/outcomes/persistence.py (Refactor item 16)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.db import funds, get_engine, outcomes, partners
from core.outcomes.events import OutcomeEvent
from core.outcomes.persistence import (
    is_duplicate_event,
    is_unchanged_from_latest,
    persist_outcome_event,
)


_OBS = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path: Path):
    """Isolated SQLite + a fund/partner parent row so outcomes' FK
    constraint is satisfied."""
    db_path = tmp_path / "test.db"
    eng = get_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="Test", domain="t.example", is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="p1", fund_id="f1", name="Test Partner",
        ))
    return eng


def _event(**over) -> OutcomeEvent:
    base = dict(
        partner_id="p1",
        outreach_status="sent",
        reply_type=None,
        meeting_booked=False,
        meeting_date=None,
        meeting_outcome=None,
        source="attio",
        external_event_id="attio:abc123",
        observed_at=_OBS,
    )
    base.update(over)
    return OutcomeEvent(**base)


# ----- is_duplicate_event -----


def test_duplicate_check_returns_false_for_fresh_id(engine) -> None:
    assert is_duplicate_event(engine, "attio:never_seen") is False


def test_duplicate_check_returns_true_after_insert(engine) -> None:
    persist_outcome_event(engine, _event())
    assert is_duplicate_event(engine, "attio:abc123") is True


def test_duplicate_check_empty_id_returns_false(engine) -> None:
    """Defensive: an event without an external_event_id should not
    match any row (and never match all rows that happen to be NULL)."""
    assert is_duplicate_event(engine, "") is False


# ----- is_unchanged_from_latest -----


def test_unchanged_returns_false_when_no_prior_outcome(engine) -> None:
    assert is_unchanged_from_latest(engine, _event()) is False


def test_unchanged_returns_true_when_state_matches_latest(engine) -> None:
    persist_outcome_event(engine, _event())
    # Same state, different external_event_id (e.g. webhook + cron
    # observed the same state).
    same_state = _event(external_event_id="attio:other_id")
    assert is_unchanged_from_latest(engine, same_state) is True


def test_unchanged_returns_false_when_outreach_status_changed(engine) -> None:
    persist_outcome_event(engine, _event(outreach_status="sent"))
    changed = _event(
        outreach_status="replied", external_event_id="attio:next",
    )
    assert is_unchanged_from_latest(engine, changed) is False


def test_unchanged_returns_false_when_meeting_booked_changed(engine) -> None:
    persist_outcome_event(engine, _event(meeting_booked=False))
    changed = _event(
        meeting_booked=True, external_event_id="attio:next",
    )
    assert is_unchanged_from_latest(engine, changed) is False


def test_unchanged_compares_against_LATEST_not_any(engine) -> None:
    """If an older outcome had state S1, a NEWER outcome has S2,
    and a third event arrives with state S1 -- it's NOT unchanged
    (S1 != S2 = latest)."""
    persist_outcome_event(engine, _event(
        outreach_status="sent", external_event_id="attio:1",
    ))
    persist_outcome_event(engine, _event(
        outreach_status="replied", external_event_id="attio:2",
    ))
    revert = _event(outreach_status="sent", external_event_id="attio:3")
    # State went sent -> replied; another sent observation is a real
    # change from the LATEST row.
    assert is_unchanged_from_latest(engine, revert) is False


# ----- persist_outcome_event -----


def test_persist_inserts_when_new(engine) -> None:
    outcome_id = persist_outcome_event(engine, _event())
    assert outcome_id is not None
    with engine.begin() as conn:
        rows = list(conn.execute(select(outcomes.c.partner_id)))
    assert rows == [("p1",)]


def test_persist_skips_duplicate_event_id(engine) -> None:
    persist_outcome_event(engine, _event())
    second = persist_outcome_event(engine, _event())  # same id
    assert second is None
    # Only one row total.
    with engine.begin() as conn:
        count = conn.execute(
            select(outcomes.c.outcome_id),
        ).fetchall()
    assert len(count) == 1


def test_persist_skips_when_state_unchanged(engine) -> None:
    persist_outcome_event(engine, _event())
    # Different external_event_id but same state -> still skipped.
    second = persist_outcome_event(
        engine, _event(external_event_id="attio:other"),
    )
    assert second is None


def test_persist_inserts_when_state_changed(engine) -> None:
    persist_outcome_event(engine, _event(outreach_status="sent"))
    next_id = persist_outcome_event(
        engine, _event(
            outreach_status="replied", external_event_id="attio:next",
        ),
    )
    assert next_id is not None
    with engine.begin() as conn:
        statuses = [
            r[0] for r in conn.execute(
                select(outcomes.c.outreach_status)
                .order_by(outcomes.c.outcome_id)
            )
        ]
    assert statuses == ["sent", "replied"]


def test_event_observed_at_lands_as_synced_at(engine) -> None:
    """The OutcomeEvent.observed_at maps to outcomes.synced_from_attio_at
    -- the legacy column name is kept for back-compat even though the
    source may not be Attio."""
    persist_outcome_event(engine, _event(observed_at=_OBS))
    with engine.begin() as conn:
        row = conn.execute(
            select(outcomes.c.synced_from_attio_at, outcomes.c.source)
        ).first()
    # SQLite drops tz; compare wall-clock fields.
    assert row[0].year == 2026
    assert row[0].month == 5
    assert row[1] == "attio"


def test_persist_carries_meeting_date(engine) -> None:
    ev = _event(meeting_date=date(2026, 6, 1), meeting_booked=True)
    persist_outcome_event(engine, ev)
    with engine.begin() as conn:
        row = conn.execute(
            select(outcomes.c.meeting_date, outcomes.c.meeting_booked)
        ).first()
    assert row[0] == date(2026, 6, 1)
    assert bool(row[1]) is True
