"""Unit tests for core/outcomes/attio_adapter.py (Refactor item 16)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.outcomes.attio_adapter import attio_record_to_event
from core.outcomes.events import OutcomeEvent


_OBS = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
_MAP = {"attio_rec_42": "partner_p1"}


def _rec(record_id="attio_rec_42", **value_overrides) -> dict:
    """Build a minimal Attio person record. Each value override is
    wrapped in the appropriate list-of-dicts shape based on type."""
    values: dict = {}
    for k, v in value_overrides.items():
        if v is None:
            values[k] = None
        elif isinstance(v, bool):
            values[k] = [{"value": v}]
        elif isinstance(v, dict) and "option" in v:
            # caller already wrapped as option shape
            values[k] = [v]
        else:
            # default: scalar value shape
            values[k] = [{"value": v}]
    return {"id": {"record_id": record_id}, "values": values}


def _option(title: str) -> dict:
    return {"option": {"title": title}}


def test_attio_record_with_full_state_produces_event() -> None:
    rec = _rec(
        outreach_status=_option("sent"),
        reply_type=_option("passed_not_a_fit"),
        meeting_booked=False,
        meeting_date="2026-04-01",
        meeting_outcome=_option("not_a_fit"),
    )
    ev = attio_record_to_event(rec, _MAP, _OBS)
    assert isinstance(ev, OutcomeEvent)
    assert ev.partner_id == "partner_p1"
    assert ev.outreach_status == "sent"
    assert ev.reply_type == "passed_not_a_fit"
    assert ev.meeting_booked is False
    assert ev.meeting_date == date(2026, 4, 1)
    assert ev.meeting_outcome == "not_a_fit"
    assert ev.source == "attio"
    assert ev.observed_at == _OBS
    assert ev.external_event_id.startswith("attio:")


def test_unknown_record_id_returns_none() -> None:
    rec = _rec(record_id="not_in_map")
    assert attio_record_to_event(rec, _MAP, _OBS) is None


def test_missing_record_id_returns_none() -> None:
    """Defense against Attio shape drift -- the id sub-object may
    arrive without a record_id key."""
    rec = {"id": {}, "values": {}}
    assert attio_record_to_event(rec, _MAP, _OBS) is None


def test_string_false_meeting_booked_does_not_truthy() -> None:
    """Regression: Attio sometimes returns 'false' as a string. The
    legacy bool() coercion treated that as True."""
    rec = _rec(meeting_booked="false")
    ev = attio_record_to_event(rec, _MAP, _OBS)
    assert ev is not None
    assert ev.meeting_booked is False


def test_empty_values_block_yields_all_nulls() -> None:
    rec = {"id": {"record_id": "attio_rec_42"}, "values": {}}
    ev = attio_record_to_event(rec, _MAP, _OBS)
    assert ev is not None
    assert ev.outreach_status is None
    assert ev.reply_type is None
    assert ev.meeting_booked is False
    assert ev.meeting_date is None
    assert ev.meeting_outcome is None


def test_malformed_meeting_date_falls_back_to_none() -> None:
    """A garbage date string shouldn't blow up the whole adapter --
    the field becomes None and the rest of the event still lands."""
    rec = _rec(meeting_date="not-a-date")
    ev = attio_record_to_event(rec, _MAP, _OBS)
    assert ev is not None
    assert ev.meeting_date is None


def test_event_id_stable_for_same_state() -> None:
    """Re-running the same observation produces the same
    external_event_id so the persistence dedup catches retries."""
    rec1 = _rec(
        outreach_status=_option("sent"),
        meeting_booked=False,
    )
    rec2 = _rec(
        outreach_status=_option("sent"),
        meeting_booked=False,
    )
    a = attio_record_to_event(rec1, _MAP, _OBS)
    b = attio_record_to_event(rec2, _MAP, _OBS)
    assert a is not None and b is not None
    assert a.external_event_id == b.external_event_id


def test_event_id_changes_when_state_changes() -> None:
    rec_sent = _rec(outreach_status=_option("sent"))
    rec_replied = _rec(outreach_status=_option("replied"))
    a = attio_record_to_event(rec_sent, _MAP, _OBS)
    b = attio_record_to_event(rec_replied, _MAP, _OBS)
    assert a.external_event_id != b.external_event_id


def test_event_id_distinguishes_record_ids() -> None:
    """Two different Attio records in the same state must produce
    different external_event_ids so two partners' identical states
    don't collide."""
    mapping = {"r_a": "p1", "r_b": "p2"}
    a = attio_record_to_event(
        _rec(record_id="r_a", outreach_status=_option("sent")),
        mapping, _OBS,
    )
    b = attio_record_to_event(
        _rec(record_id="r_b", outreach_status=_option("sent")),
        mapping, _OBS,
    )
    assert a.external_event_id != b.external_event_id
