"""B2 (Coach Sent flow): polling Gmail's Sent box and persisting
touchpoints into `outreach_events`.

Layout:
  - `poll_gmail_sent_for_workspace(ws)` is the unit of work the
    `POST /api/public/hooks/poll-gmail-sent` endpoint invokes per
    tenant. Returns the number of new events inserted.
  - `latest_event_at(engine, *, source, event_type)` -> the
    high-water mark we resume polling from (Gmail's `after:` query
    operator takes a unix timestamp).
  - `record_gmail_sent(engine, *, msg, partner_id)` is the upsert
    primitive that idempotently inserts an event row keyed on
    `(source, external_id)`.

Why a separate module: keeps the polling pipeline testable without
spinning up FastAPI, and gives Phase B3's reply poller a place to
live next to it (same Gmail client, same upsert primitive).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import desc, func, select

from core.db import outreach_events, partners, upsert


# A poll pass reports back per-tenant so the hook caller can log /
# alert on partial failures. `errors` is a list of string reasons
# (one per workspace that 5xx'd); the rest succeed.
@dataclass(frozen=True)
class PollResult:
    workspace: str  # workspace path or user_id, whichever the caller stamped
    inserted: int
    error: Optional[str] = None


def latest_event_at(
    engine: Any, *, source: str, event_type: str,
) -> Optional[_dt.datetime]:
    """High-water mark for resuming a poll loop. Returns None when
    no events of this (source, event_type) have ever been recorded
    (first-run case -- caller decides the lookback window)."""
    with engine.begin() as conn:
        row = conn.execute(
            select(func.max(outreach_events.c.occurred_at)).where(
                outreach_events.c.source == source,
                outreach_events.c.event_type == event_type,
            )
        ).first()
    if row is None or row[0] is None:
        return None
    val = row[0]
    return val if isinstance(val, _dt.datetime) else None


def partner_by_email_lookup(engine: Any) -> dict[str, str]:
    """Lowercased-email -> partner_id map for matching Gmail
    recipients to the local partner row. Built once per poll pass
    (vs. one query per event) since a typical pass touches dozens
    of recipients but only a few hundred partners total."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(partners.c.partner_id, partners.c.email)
        )
        return {
            (r.email or "").strip().lower(): r.partner_id
            for r in rows
            if r.email
        }


def record_gmail_sent(
    engine: Any, *,
    external_id: str,
    thread_id: Optional[str],
    occurred_at: _dt.datetime,
    recipient_email: Optional[str],
    subject: Optional[str],
    body_snippet: Optional[str],
    partner_id: Optional[str],
    draft_id: Optional[int],
) -> bool:
    """Idempotent insert. Returns True when a new row landed,
    False when the (source='gmail', external_id) was already
    present.

    The UNIQUE index on (source, external_id) is what enforces
    idempotency; we still check beforehand so the return value is
    honest (SQLite's `on_conflict_do_nothing` doesn't surface a
    rowcount that distinguishes 'inserted' from 'collided').
    """
    with engine.begin() as conn:
        existing = conn.execute(
            select(outreach_events.c.event_id).where(
                outreach_events.c.source == "gmail",
                outreach_events.c.external_id == external_id,
            )
        ).first()
        if existing is not None:
            return False
        upsert(
            conn, outreach_events,
            # event_id is autoincrement; the UNIQUE we care about is
            # (source, external_id) but upsert() requires the actual
            # PK. Use the existence check above + a plain insert here.
            ["event_id"],
            {
                "source": "gmail",
                "event_type": "sent",
                "external_id": external_id,
                "thread_id": thread_id,
                "occurred_at": occurred_at,
                "recipient_email": recipient_email,
                "subject": subject,
                "body_snippet": body_snippet,
                "partner_id": partner_id,
                "draft_id": draft_id,
                "unread": False,
                "created_at": _dt.datetime.now(_dt.timezone.utc),
            },
        )
    return True


# Default lookback when a workspace has never been polled. 14 days
# is wide enough to catch outreach the operator started before
# Kismet was installed but narrow enough to keep the first poll
# bounded (Gmail's q=after: is a list call we then batch-get).
_FIRST_RUN_LOOKBACK_DAYS = 14


def poll_gmail_sent_for_workspace(ws, gmail_client_factory=None) -> PollResult:
    """Poll one workspace's Gmail Sent box and persist new send
    events.

    `gmail_client_factory` is injected for tests; defaults to
    `core.gmail_client.GmailClient.from_workspace_polling`. When
    the workspace has no Gmail token at all, returns inserted=0
    with no error (this is the steady state for tenants that
    haven't connected Gmail yet).
    """
    if gmail_client_factory is None:
        from core.gmail_client import GmailClient
        gmail_client_factory = GmailClient.from_workspace_polling

    ws_path_str = str(getattr(ws, "path", ws))

    try:
        client = gmail_client_factory(ws)
    except FileNotFoundError:
        # No token on file -- tenant hasn't connected Gmail. Not
        # an error; the steady state for fresh workspaces.
        return PollResult(workspace=ws_path_str, inserted=0)
    except Exception as exc:  # noqa: BLE001 - diverse google errors
        return PollResult(
            workspace=ws_path_str,
            inserted=0,
            error=f"gmail_client_unavailable: {exc}",
        )

    from core.db import get_engine
    engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    hwm = latest_event_at(
        engine, source="gmail", event_type="sent",
    )
    if hwm is None:
        after_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=_FIRST_RUN_LOOKBACK_DAYS,
        )
    else:
        # Subtract one second so we don't miss messages that share
        # the same epoch second as our last-seen one (Gmail's
        # after: is inclusive-of-the-second).
        after_dt = hwm - _dt.timedelta(seconds=1)

    try:
        messages = list(client.list_sent_since(after_dt))
    except Exception as exc:  # noqa: BLE001
        return PollResult(
            workspace=ws_path_str,
            inserted=0,
            error=f"gmail_list_failed: {exc}",
        )

    partner_by_email = partner_by_email_lookup(engine)
    inserted = 0
    for msg in messages:
        # `list_sent_since` returns dicts (not Gmail SDK types) so
        # tests can fixture them easily. Fields:
        #   external_id, thread_id, occurred_at, recipient_email,
        #   subject, body_snippet
        recipient = (msg.get("recipient_email") or "").strip().lower()
        partner_id = partner_by_email.get(recipient) if recipient else None
        ok = record_gmail_sent(
            engine,
            external_id=msg["external_id"],
            thread_id=msg.get("thread_id"),
            occurred_at=msg["occurred_at"],
            recipient_email=msg.get("recipient_email"),
            subject=msg.get("subject"),
            body_snippet=msg.get("body_snippet"),
            partner_id=partner_id,
            draft_id=None,
        )
        if ok:
            inserted += 1

    return PollResult(workspace=ws_path_str, inserted=inserted)


def list_sent_events(engine: Any, *, limit: int = 100) -> list[dict]:
    """Read path for `GET /sent`. Joins event -> partner so the
    frontend gets a partner-friendly view (name, email) without a
    second round-trip.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                outreach_events.c.event_id,
                outreach_events.c.partner_id,
                outreach_events.c.draft_id,
                outreach_events.c.external_id,
                outreach_events.c.thread_id,
                outreach_events.c.occurred_at,
                outreach_events.c.recipient_email,
                outreach_events.c.subject,
                outreach_events.c.body_snippet,
                partners.c.name.label("partner_name"),
                partners.c.email.label("partner_email"),
            )
            .select_from(
                outreach_events.outerjoin(
                    partners,
                    partners.c.partner_id == outreach_events.c.partner_id,
                )
            )
            .where(
                outreach_events.c.source == "gmail",
                outreach_events.c.event_type == "sent",
            )
            .order_by(desc(outreach_events.c.occurred_at))
            .limit(limit)
        )
        return [dict(r._mapping) for r in rows]
