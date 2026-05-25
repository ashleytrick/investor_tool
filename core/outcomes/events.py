"""Source-neutral outcome event (Refactor item 16).

`OutcomeEvent` is the wire format between source-specific adapters
(Attio today; webhooks / Gmail / manual CSV in the future) and the
local `outcomes` table. Adapters return events; the persistence layer
dedups via external_event_id and inserts.

The shape mirrors the brief's outcomes schema with two additions:
  - `source` : a short string tag ("attio", "manual_csv", ...) so
    downstream queries can filter by ingestion source.
  - `external_event_id` : a source-prefixed stable identifier --
    typically "<source>:<sha1[:16]>" hashed over the meaningful
    state fields. Two re-runs of the same source on the same state
    must produce the same id so the dedup catches retries / cron
    overlaps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class OutcomeEvent:
    partner_id: str
    outreach_status: str | None
    reply_type: str | None
    meeting_booked: bool
    meeting_date: date | None
    meeting_outcome: str | None
    source: str
    external_event_id: str
    observed_at: datetime

    def to_row_values(self) -> dict:
        """Return a dict ready for outcomes.insert().values(**...).
        The outcomes table uses synced_from_attio_at for the legacy
        ingestion timestamp column; we keep that name for back-compat
        even though the source may not be Attio."""
        return {
            "partner_id": self.partner_id,
            "outreach_status": self.outreach_status,
            "reply_type": self.reply_type,
            "meeting_booked": self.meeting_booked,
            "meeting_date": self.meeting_date,
            "meeting_outcome": self.meeting_outcome,
            "synced_from_attio_at": self.observed_at,
            "source": self.source,
            "external_event_id": self.external_event_id,
        }
