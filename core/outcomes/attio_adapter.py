"""Attio-records -> OutcomeEvent adapter (Refactor item 16).

Translates the records returned by `client.query_records_all(...)`
into source-neutral `OutcomeEvent` instances the persistence layer
can dedup + insert.

`attio_record_to_event(record, attio_to_partner, observed_at) ->
OutcomeEvent | None` is the entry point. Returns None when the
incoming record's record_id can't be mapped to a known local
partner_id -- caller should skip those silently (they're partners
the operator hasn't synced from Stage 8 yet, or external Attio
records the workspace shouldn't observe).

The value-extraction helpers (_scalar / _option_title / _bool /
_date) are kept here -- not exported to a general "attio_values"
module -- because they're Attio's v2 record-values shape, not a
common pattern across sources.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Mapping

from core.outcomes.events import OutcomeEvent


def _scalar(values: dict, slug: str) -> Any:
    v = values.get(slug)
    if not v:
        return None
    if isinstance(v, list):
        return v[0].get("value") if v else None
    return v


def _bool(values: dict, slug: str) -> bool:
    """Parse a boolean Attio attribute safely.

    Finding 33: a previous version did `bool(_scalar(...))`, which
    accepted the string 'false' from the API as Truthy. We map common
    falsey strings explicitly and only treat real-bool / explicit
    truthy strings as True.
    """
    v = _scalar(values, slug)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return False


def _option_title(values: dict, slug: str) -> str | None:
    v = values.get(slug)
    if not v:
        return None
    try:
        return v[0]["option"]["title"]
    except (KeyError, IndexError, TypeError):
        return None


def _date(values: dict, slug: str) -> date | None:
    raw = _scalar(values, slug)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


def _event_id_for(
    record_id: str,
    outreach_status: str | None,
    reply_type: str | None,
    meeting_booked: bool,
    meeting_date: date | None,
    meeting_outcome: str | None,
) -> str:
    """Stable hash over (record_id + meaningful state fields).

    Attio's record id alone isn't unique per OUTCOME event because the
    same person record is modified many times. Hashing over the
    record_id PLUS the state fields produces a token that's identical
    across cron-overlap retries on the SAME observed state, and
    different the moment the state changes.
    """
    payload = "|".join((
        str(record_id),
        str(outreach_status),
        str(reply_type),
        str(meeting_booked),
        str(meeting_date),
        str(meeting_outcome),
    ))
    return "attio:" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def attio_record_to_event(
    record: dict,
    attio_to_partner: Mapping[str, str],
    observed_at: datetime,
) -> OutcomeEvent | None:
    """Map one Attio person record to an OutcomeEvent.

    Returns None when:
      - the record's record_id is missing (Attio shape drift), OR
      - the record_id doesn't map to a known local partner_id (the
        workspace hasn't synced this partner via Stage 8 yet).

    Callers (the cron job orchestrator) should `skip` those records
    rather than fail.
    """
    rec_id = (record.get("id") or {}).get("record_id")
    if not rec_id:
        return None
    pid = attio_to_partner.get(rec_id)
    if not pid:
        return None
    values = record.get("values", {})
    outreach_status = _option_title(values, "outreach_status")
    reply_type = _option_title(values, "reply_type")
    meeting_booked = _bool(values, "meeting_booked")
    meeting_date = _date(values, "meeting_date")
    meeting_outcome = _option_title(values, "meeting_outcome")
    return OutcomeEvent(
        partner_id=pid,
        outreach_status=outreach_status,
        reply_type=reply_type,
        meeting_booked=meeting_booked,
        meeting_date=meeting_date,
        meeting_outcome=meeting_outcome,
        source="attio",
        external_event_id=_event_id_for(
            rec_id, outreach_status, reply_type,
            meeting_booked, meeting_date, meeting_outcome,
        ),
        observed_at=observed_at,
    )
