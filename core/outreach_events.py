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


def draft_id_by_partner_lookup(engine: Any) -> dict[str, int]:
    """Latest non-superseded draft_id per partner -- the best guess
    at "which draft did this sent event come from" for an outbound
    Gmail message. (Review item #18.)

    Stage 7 supersedes prior drafts so there's typically one live
    row per partner. We pick the highest draft_id (autoincrement)
    among non-superseded rows to handle the rare multi-draft case
    deterministically.
    """
    from sqlalchemy import func as _sqlfunc
    from core.db import email_drafts
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                email_drafts.c.partner_id,
                _sqlfunc.max(email_drafts.c.draft_id).label("draft_id"),
            )
            .where(email_drafts.c.superseded_at.is_(None))
            .group_by(email_drafts.c.partner_id)
        )
        return {
            r.partner_id: int(r.draft_id)
            for r in rows
            if r.partner_id and r.draft_id is not None
        }


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
    draft_by_partner = draft_id_by_partner_lookup(engine)
    inserted = 0
    for msg in messages:
        # `list_sent_since` returns dicts (not Gmail SDK types) so
        # tests can fixture them easily. Fields:
        #   external_id, thread_id, occurred_at, recipient_email,
        #   subject, body_snippet
        recipient = (msg.get("recipient_email") or "").strip().lower()
        partner_id = partner_by_email.get(recipient) if recipient else None
        # Review item #18: link the sent event to the latest
        # non-superseded draft for the partner so the audit trail
        # answers "which draft did this come from."
        draft_id = (
            draft_by_partner.get(partner_id) if partner_id else None
        )
        ok = record_gmail_sent(
            engine,
            external_id=msg["external_id"],
            thread_id=msg.get("thread_id"),
            occurred_at=msg["occurred_at"],
            recipient_email=msg.get("recipient_email"),
            subject=msg.get("subject"),
            body_snippet=msg.get("body_snippet"),
            partner_id=partner_id,
            draft_id=draft_id,
        )
        if ok:
            inserted += 1

    return PollResult(workspace=ws_path_str, inserted=inserted)


# ---------- B3: replies ----------

# Lightweight heuristic classifier so the frontend has something to
# render on day 1. Replace with a Claude call later once we have a
# labeled set; for now this surfaces the obviously-positive /
# obviously-negative cases and leaves the rest as 'unclear' (the
# operator reads those manually anyway).
#
# Order matters: meeting_booked beats interested beats pass beats
# unclear -- "Sure, happy to chat. Booking link below." should
# classify as meeting_booked, not interested.
_MEETING_BOOKED_HINTS = (
    "calendly.com/",
    "cal.com/",
    "savvycal.com/",
    "book a time",
    "book a call",
    "schedule a call",
    "schedule a time",
    "schedule a chat",
    "happy to chat",
    "let's grab",
    "let's set",
    "works for me",
)
_INTERESTED_HINTS = (
    "tell me more",
    "would love to learn",
    "this is interesting",
    "i'm interested",
    "send me",
    "share the deck",
    "send the deck",
    "loop me in",
    "let's talk",
    "would love to chat",
)
_PASS_HINTS = (
    "not a fit",
    "passing on this",
    "we'll pass",
    "we will pass",
    "we'll have to pass",
    "out of scope",
    "out of thesis",
    "not investing",
    "too early",
    "too late",
    "not the right",
    "best of luck",
    "good luck with the round",
    "please remove me",
    "unsubscribe",
)


_VALID_CLASSES = ("meeting_booked", "interested", "pass", "unclear")


def _classify_reply_heuristic(body_snippet: str | None) -> str:
    """Cheap lowercase-substring classifier. Same precedence as the
    docstring on `classify_reply`. Always returns a label."""
    if not body_snippet:
        return "unclear"
    text = body_snippet.lower()
    for hint in _MEETING_BOOKED_HINTS:
        if hint in text:
            return "meeting_booked"
    for hint in _INTERESTED_HINTS:
        if hint in text:
            return "interested"
    for hint in _PASS_HINTS:
        if hint in text:
            return "pass"
    return "unclear"


def classify_reply(
    body_snippet: str | None, *, llm: Any = None,
) -> str:
    """Returns one of 'meeting_booked' | 'interested' | 'pass' |
    'unclear' from a Gmail snippet.

    Two-stage classifier (review item #19):
      1. Cheap heuristic via lowercase substring match. If it
         resolves to a non-unclear label, return immediately --
         the obvious cases don't need an LLM round-trip.
      2. When the heuristic says 'unclear' AND a live LLM client
         is provided, ask Claude. Falls back to 'unclear' on any
         model error (we never block the polling pipeline on
         classification flake).

    The two-stage shape keeps cost / latency low for typical
    inboxes (most replies are obvious) and reserves the LLM for
    the genuinely ambiguous cases the operator would otherwise
    have to read by hand.

    Operator-facing UI should still let humans override the
    auto-label; classification is a hint, not ground truth.
    """
    heuristic_label = _classify_reply_heuristic(body_snippet)
    if heuristic_label != "unclear":
        return heuristic_label
    if llm is None or not body_snippet:
        return "unclear"
    # Stub mode: the LLM client raises if no stub_response is
    # provided. Wrap in try / except so unexpected stub-mode
    # invocation falls back to the heuristic label rather than
    # crashing the whole poll pass.
    try:
        return _classify_reply_via_llm(llm, body_snippet)
    except Exception:  # noqa: BLE001 - classification flake -> heuristic
        return "unclear"


def _classify_reply_via_llm(llm: Any, body_snippet: str) -> str:
    """Single Claude call. Returns one of the four valid labels;
    anything unexpected collapses to 'unclear'."""
    from pydantic import BaseModel, Field

    class _ReplyClassification(BaseModel):
        label: str = Field(
            description=(
                "One of: meeting_booked, interested, pass, "
                "unclear (lowercase, underscored)."
            ),
        )

    prompt = (
        "You are classifying a single email reply from a VC "
        "partner to a founder's cold outreach. Return strict "
        "JSON: {\"label\": \"<one_of>\"}\n"
        "\n"
        "Labels (use exactly one):\n"
        "  - meeting_booked: the partner agreed to a meeting "
        "or shared a calendar link.\n"
        "  - interested: the partner asked for more info, the "
        "deck, intros, or signaled curiosity.\n"
        "  - pass: the partner declined, said not a fit, "
        "or asked to be removed from outreach.\n"
        "  - unclear: anything else (autoresponders, OOO, "
        "ambiguous polite responses).\n"
        "\n"
        f"Reply snippet:\n{body_snippet[:1500]}\n"
        "\n"
        "Return the JSON object only."
    )
    # Stub mode behavior: caller must provide stub_response.
    # We pass the heuristic's fallback so tests in stub mode
    # exercise the classifier path without a real model.
    result = llm.complete_json(
        prompt=prompt,
        schema=_ReplyClassification,
        max_tokens=64,
        stub_response={"label": "unclear"},
    )
    label = (result.label or "").strip().lower()
    return label if label in _VALID_CLASSES else "unclear"


def record_gmail_reply(
    engine: Any, *,
    external_id: str,
    thread_id: Optional[str],
    occurred_at: _dt.datetime,
    sender_email: Optional[str],
    subject: Optional[str],
    body_snippet: Optional[str],
    partner_id: Optional[str],
    draft_id: Optional[int],
    unread: bool,
    classification: str,
) -> bool:
    """Idempotent insert of a reply event. Same shape as
    record_gmail_sent but with event_type='replied' + classification
    + unread.
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
            ["event_id"],
            {
                "source": "gmail",
                "event_type": "replied",
                "external_id": external_id,
                "thread_id": thread_id,
                "occurred_at": occurred_at,
                # For replies, the partner is the *sender*. Store
                # in recipient_email for column symmetry.
                "recipient_email": sender_email,
                "subject": subject,
                "body_snippet": body_snippet,
                "partner_id": partner_id,
                "draft_id": draft_id,
                "unread": unread,
                "classification": classification,
                "created_at": _dt.datetime.now(_dt.timezone.utc),
            },
        )
    return True


def _sent_thread_ids(engine: Any) -> list[str]:
    """Thread IDs from sent events -- the universe of conversations
    a reply could land in."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(outreach_events.c.thread_id)
            .where(
                outreach_events.c.source == "gmail",
                outreach_events.c.event_type == "sent",
                outreach_events.c.thread_id.is_not(None),
            )
            .distinct()
        )
        return [r.thread_id for r in rows if r.thread_id]


def _partner_by_sent_thread(engine: Any) -> dict[str, str]:
    """thread_id -> partner_id from sent events. Used to attribute
    a reply whose sender isn't a direct partner email (e.g.
    `assistant@firm.com` replying for `partner@firm.com`)."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                outreach_events.c.thread_id,
                outreach_events.c.partner_id,
            )
            .where(
                outreach_events.c.source == "gmail",
                outreach_events.c.event_type == "sent",
                outreach_events.c.partner_id.is_not(None),
                outreach_events.c.thread_id.is_not(None),
            )
        )
        return {r.thread_id: r.partner_id for r in rows if r.thread_id}


def poll_gmail_replies_for_workspace(
    ws, gmail_client_factory=None, llm_factory=None,
) -> PollResult:
    """Poll one workspace's inbox for replies in threads we've sent
    into. Mirrors poll_gmail_sent_for_workspace -- same factory
    contract, same idempotency, same per-tenant error capture.

    `llm_factory` is injected for tests (review item #19). When
    omitted, defaults to `LLMClient(workspace=ws)`. In stub mode
    (no ANTHROPIC_API_KEY) the classifier falls back to the
    heuristic, which is the same behavior as pre-#19.
    """
    if gmail_client_factory is None:
        from core.gmail_client import GmailClient
        gmail_client_factory = GmailClient.from_workspace_polling
    if llm_factory is None:
        def _default_llm_factory(workspace):
            try:
                from core.llm.client import LLMClient
                return LLMClient(workspace=workspace)
            except Exception:  # noqa: BLE001 - never fail the poll on LLM
                return None
        llm_factory = _default_llm_factory

    ws_path_str = str(getattr(ws, "path", ws))

    try:
        client = gmail_client_factory(ws)
    except FileNotFoundError:
        return PollResult(workspace=ws_path_str, inserted=0)
    except Exception as exc:  # noqa: BLE001
        return PollResult(
            workspace=ws_path_str,
            inserted=0,
            error=f"gmail_client_unavailable: {exc}",
        )

    from core.db import get_engine
    engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    hwm = latest_event_at(
        engine, source="gmail", event_type="replied",
    )
    if hwm is None:
        after_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=_FIRST_RUN_LOOKBACK_DAYS,
        )
    else:
        after_dt = hwm - _dt.timedelta(seconds=1)

    thread_ids = _sent_thread_ids(engine)
    if not thread_ids:
        # No sent events yet -> nothing to reconcile against.
        return PollResult(workspace=ws_path_str, inserted=0)

    try:
        messages = list(
            client.list_replies_since(after_dt, thread_ids=thread_ids)
        )
    except Exception as exc:  # noqa: BLE001
        return PollResult(
            workspace=ws_path_str,
            inserted=0,
            error=f"gmail_list_failed: {exc}",
        )

    partner_by_email = partner_by_email_lookup(engine)
    partner_by_thread = _partner_by_sent_thread(engine)
    # Build the LLM client once per poll pass (one auth + one
    # stub-check, then reused across every reply). Best-effort:
    # any failure here -> llm stays None -> classifier falls
    # back to the heuristic.
    try:
        llm = llm_factory(ws)
    except Exception:  # noqa: BLE001
        llm = None
    inserted = 0
    for msg in messages:
        sender = (msg.get("recipient_email") or "").strip().lower()
        thread_id = msg.get("thread_id")
        partner_id = (
            partner_by_email.get(sender) if sender else None
        ) or partner_by_thread.get(thread_id)
        classification = classify_reply(
            msg.get("body_snippet"), llm=llm,
        )
        ok = record_gmail_reply(
            engine,
            external_id=msg["external_id"],
            thread_id=thread_id,
            occurred_at=msg["occurred_at"],
            sender_email=msg.get("recipient_email"),
            subject=msg.get("subject"),
            body_snippet=msg.get("body_snippet"),
            partner_id=partner_id,
            draft_id=None,
            unread=bool(msg.get("unread", False)),
            classification=classification,
        )
        if ok:
            inserted += 1

    return PollResult(workspace=ws_path_str, inserted=inserted)


def list_reply_events(
    engine: Any, *,
    limit: int = 100,
    unread_only: bool = False,
) -> list[dict]:
    """Read path for `GET /replies`. Joins event -> partner and
    optionally filters to unread (the inbox-style view)."""
    stmt = (
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
            outreach_events.c.classification,
            outreach_events.c.unread,
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
            outreach_events.c.event_type == "replied",
        )
        .order_by(desc(outreach_events.c.occurred_at))
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(outreach_events.c.unread.is_(True))
    with engine.begin() as conn:
        rows = conn.execute(stmt)
        return [dict(r._mapping) for r in rows]


def mark_reply_read(engine: Any, *, event_id: int) -> bool:
    """Set `unread = false` for a reply event. Returns True when a
    row was updated, False when no matching unread reply existed."""
    with engine.begin() as conn:
        res = conn.execute(
            outreach_events.update()
            .where(
                outreach_events.c.event_id == event_id,
                outreach_events.c.event_type == "replied",
                outreach_events.c.unread.is_(True),
            )
            .values(unread=False)
        )
        return (res.rowcount or 0) > 0


@dataclass(frozen=True)
class ReconcileResult:
    workspace: str
    unread_replies: int
    # FR-4b: count of sequences this pass auto-stopped on reply.
    # The actual mutation is in core.sequences.auto_stop_sequence_if_active,
    # gated by cadence_settings.auto_stop_on_reply. Exposed in
    # the result so the hook log shows what fired.
    sequences_stopped: int = 0
    error: Optional[str] = None


def reconcile_drafts_for_workspace(ws) -> ReconcileResult:
    """Cron-triggered reconciliation pass. Surfaces the count of
    unread reply events per workspace so the hook caller can
    monitor / alert on inbox backlog.

    FR-4b: also auto-stops the partner's sequence on the first
    reply (when `cadence_settings.auto_stop_on_reply=True`). The
    helper is idempotent so re-running this hook on the same
    backlog is safe; the first stop wins on `stopped_reason`.
    """
    ws_path_str = str(getattr(ws, "path", ws))
    try:
        from core.db import get_engine
        engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    except Exception as exc:  # noqa: BLE001
        return ReconcileResult(
            workspace=ws_path_str,
            unread_replies=0,
            error=f"engine_failed: {exc}",
        )
    from core.db import sequences as _sequences_tbl
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        row = conn.execute(
            select(func.count())
            .select_from(outreach_events)
            .where(
                outreach_events.c.source == "gmail",
                outreach_events.c.event_type == "replied",
                outreach_events.c.unread.is_(True),
            )
        ).first()
        # FR-4b: auto-stop only on replies that arrived AFTER the
        # active sequence was created. Without this filter, a newly
        # captured partner who happened to email the operator months
        # ago would be auto-stopped the instant `/investors/capture`
        # seeded their sequence. The post-audit fix joins to
        # `sequences.created_at` so we only consider new-relative-to-
        # this-sequence reply events.
        #
        # The helper is still idempotent on the sequence side -- once
        # stopped, subsequent passes are no-ops -- so we don't have
        # to track "which reply we already processed". `distinct()`
        # on partner_id keeps us from calling the helper N times
        # for one partner with N replies.
        reply_partner_ids = [
            r.partner_id for r in conn.execute(
                select(outreach_events.c.partner_id)
                .select_from(
                    outreach_events.join(
                        _sequences_tbl,
                        _sequences_tbl.c.partner_id
                        == outreach_events.c.partner_id,
                    )
                )
                .where(
                    outreach_events.c.source == "gmail",
                    outreach_events.c.event_type == "replied",
                    outreach_events.c.partner_id.is_not(None),
                    outreach_events.c.occurred_at
                    >= _sequences_tbl.c.created_at,
                )
                .distinct()
            )
        ]
        stopped = 0
        for pid in reply_partner_ids:
            if auto_stop_sequence_if_active(
                conn, partner_id=pid, reason="reply",
            ):
                stopped += 1
    n = int(row[0]) if row else 0
    return ReconcileResult(
        workspace=ws_path_str,
        unread_replies=n,
        sequences_stopped=stopped,
    )


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
