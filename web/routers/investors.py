"""Investor-management router (FR-1 frontend wins).

  PUT  /investors/{partner_id}/status   — manual pipeline-stage
                                          override. Writes the
                                          stage into the same
                                          partner_pipeline row B4
                                          writes, stamped
                                          updated_by='ui:status_picker'
                                          for audit.
  PUT  /investors/{partner_id}/channel  — set channel_pref
                                          ('email' | 'linkedin' |
                                          'both'). Persists on
                                          partners.channel_pref.
  POST /drafts/{draft_id}/snooze        — alias for POST
                                          /snoozes/{draft_id}.
                                          Frontend's setSnooze()
                                          uses this URL shape.
                                          Accepts {until: null}
                                          to clear the snooze.

Per FR-1 §10: pipeline status override policy is "local-only for
v1". Push-through to a connected CRM is deferred until we wire
the outbound write path.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from core.db import (
    draft_snoozes, email_drafts, funds, partner_pipeline, partners,
    upsert,
)
from core.ids import fund_id_for, partner_id_for
from web.deps import _engine_and_ws, require_auth


_VALID_CHANNELS = {"email", "linkedin", "both"}


class InvestorStatusBody(BaseModel):
    status: str = Field(
        min_length=1, max_length=64,
        description=(
            "Pipeline stage / status. Free-form string; "
            "conventional values: 'contacted', 'meeting_set', "
            "'passed', 'invested'."
        ),
    )


class InvestorStatusView(BaseModel):
    partner_id: str
    status: str | None = None
    updated_at: str | None = None


class InvestorChannelBody(BaseModel):
    channel_pref: str = Field(
        description="One of: email | linkedin | both",
    )


class InvestorChannelView(BaseModel):
    partner_id: str
    channel_pref: str


class SnoozeAliasBody(BaseModel):
    """Either ISO future datetime to snooze, or null to unsnooze.
    Matches the frontend's `setSnooze(draftId, untilIso | null)`
    mock shape."""
    until: str | None = Field(
        default=None,
        description=(
            "ISO datetime in the future, or null to clear an "
            "existing snooze on this draft."
        ),
    )
    reason: str | None = None


class SnoozeAliasView(BaseModel):
    draft_id: int
    snoozed_until: str | None = None
    reason: str | None = None
    created_at: str | None = None


router = APIRouter(tags=["investors"])


# ---------- status ----------

@router.put(
    "/investors/{partner_id}/status",
    response_model=InvestorStatusView,
    summary=(
        "Set the partner's pipeline status (alias for POST "
        "/partners/{id}/pipeline, stamped 'ui:status_picker')"
    ),
)
def set_investor_status(
    partner_id: str,
    body: InvestorStatusBody,
    _auth: None = Depends(require_auth),
) -> InvestorStatusView:
    """FR-1 §10 option C: local-only for v1. Writes into
    partner_pipeline so Today / review surfaces see it
    immediately. Future PR will push-through to a connected CRM."""
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(partners.c.partner_id).where(
                partners.c.partner_id == partner_id,
            )
        ).first()
        if partner_row is None:
            raise HTTPException(
                404, f"unknown partner_id: {partner_id}",
            )
        upsert(
            conn, partner_pipeline, ["partner_id"],
            {
                "partner_id": partner_id,
                "stage": body.status,
                "notes": None,
                "updated_at": now,
                "updated_by": "ui:status_picker",
            },
        )
    return InvestorStatusView(
        partner_id=partner_id,
        status=body.status,
        updated_at=now.isoformat(),
    )


# ---------- channel ----------

@router.put(
    "/investors/{partner_id}/channel",
    response_model=InvestorChannelView,
    summary="Set the partner's outreach channel preference",
)
def set_investor_channel(
    partner_id: str,
    body: InvestorChannelBody,
    _auth: None = Depends(require_auth),
) -> InvestorChannelView:
    """FR-1 §8. Persists partners.channel_pref. 422 on invalid
    values; the whitelist is server-side because a stale cached
    client could send something unsupported."""
    pref = (body.channel_pref or "").strip().lower()
    if pref not in _VALID_CHANNELS:
        raise HTTPException(
            422,
            f"channel_pref must be one of {sorted(_VALID_CHANNELS)}; "
            f"got {body.channel_pref!r}",
        )
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(partners.c.partner_id).where(
                partners.c.partner_id == partner_id,
            )
        ).first()
        if partner_row is None:
            raise HTTPException(
                404, f"unknown partner_id: {partner_id}",
            )
        conn.execute(
            partners.update()
            .where(partners.c.partner_id == partner_id)
            .values(channel_pref=pref, last_updated=now)
        )
    return InvestorChannelView(
        partner_id=partner_id, channel_pref=pref,
    )


# ---------- snooze alias ----------

def _parse_future_iso_naive_utc(value: str):
    """Parse an ISO datetime, require it's in the future, return
    tz-NAIVE UTC (the convention used end-to-end since FR fixup
    #4). Local duplicate so investors.py is self-contained and
    can ship before the coach router lands."""
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            422, f"until must be ISO 8601: {exc}",
        )
    if dt.tzinfo is None:
        utc_aware = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        utc_aware = dt.astimezone(_dt.timezone.utc)
    if utc_aware <= _dt.datetime.now(_dt.timezone.utc):
        raise HTTPException(422, "until must be in the future")
    return utc_aware.replace(tzinfo=None)


@router.post(
    "/drafts/{draft_id}/snooze",
    response_model=SnoozeAliasView,
    summary=(
        "Snooze a draft (alias for POST /snoozes/{draft_id}; "
        "pass until=null to clear)"
    ),
)
def snooze_draft_alias(
    draft_id: int,
    body: SnoozeAliasBody,
    _auth: None = Depends(require_auth),
) -> SnoozeAliasView:
    """FR-1 §9. Frontend's mockApi.setSnooze() targets
    /drafts/{id}/snooze with `{until: ISO | null}`:

      - until=null  -> clear any existing snooze, return empty view
      - until=ISO   -> 422 if past, 404 if draft doesn't exist,
                       else upsert + return the populated view
    """
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        draft_row = conn.execute(
            select(email_drafts.c.draft_id).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).first()
        if draft_row is None:
            raise HTTPException(
                404, f"unknown draft_id: {draft_id}",
            )

        if body.until is None:
            conn.execute(
                draft_snoozes.delete().where(
                    draft_snoozes.c.draft_id == draft_id,
                )
            )
            return SnoozeAliasView(draft_id=draft_id)

        snoozed_until = _parse_future_iso_naive_utc(body.until)
        upsert(
            conn, draft_snoozes, ["draft_id"],
            {
                "draft_id": draft_id,
                "snoozed_until": snoozed_until,
                "reason": body.reason,
                "created_at": now,
                "created_by": None,
            },
        )
    return SnoozeAliasView(
        draft_id=draft_id,
        snoozed_until=snoozed_until.isoformat(),
        reason=body.reason,
        created_at=now.isoformat(),
    )


# ---------- FR-1b: POST /investors/capture (QR-flow seed) ----------

_VALID_CAPTURE_CHANNELS = {"email", "linkedin", "both"}
_VALID_CAPTURE_SOURCES = {"qr", "manual", "import"}


class InvestorCaptureBody(BaseModel):
    """Frontend's `POST /investors/capture` body. Matches the
    QR-scan flow + the "manual add" flow at the same endpoint."""
    linkedin_url: str = Field(
        description=(
            "Canonical LinkedIn profile URL. De-duped per workspace; "
            "re-capturing returns the existing row with "
            "status='already_in_pipeline'."
        ),
    )
    partner_name: str = Field(min_length=1)
    firm: str = Field(
        min_length=1,
        description=(
            "Firm/fund name. We slug it to firm-slug.unclaimed if "
            "we don't already know the real domain; operators can "
            "fix the domain on the fund row later."
        ),
    )
    channel: str = Field(
        default="email",
        description="email | linkedin | both",
    )
    cadence_key: str | None = Field(
        default=None,
        description=(
            "warm | cold | NULL. Ignored in FR-1b -- the sequence "
            "seed lands in FR-3; we store the choice for then."
        ),
    )
    note: str | None = None
    source: str = Field(
        default="qr",
        description="qr | manual | import",
    )


class InvestorCaptureResult(BaseModel):
    """One investor row + a status the frontend renders."""
    status: str  # 'created' | 'already_in_pipeline'
    partner_id: str
    fund_id: str
    name: str
    firm: str
    linkedin_url: str | None = None
    channel_pref: str | None = None
    source: str | None = None
    note: str | None = None


def _slug_unclaimed_domain(firm: str) -> str:
    """firm -> firm-slug.unclaimed. Same logic as core.discovery's
    fallback used by /discovery/claim."""
    cleaned = "".join(
        ch if ch.isalnum() else "-"
        for ch in (firm or "").strip().lower()
    ).strip("-")
    return f"{cleaned}.unclaimed" if cleaned else ""


def _normalize_linkedin_url(url: str) -> str:
    """Canonicalize a LinkedIn profile URL so dedup catches the
    same profile across formatting variants.

    P2 audit fix: without this, `https://www.linkedin.com/in/jane`
    and `https://linkedin.com/in/jane/` are stored as two distinct
    partners + can later hit a partner_id collision (same firm +
    same partner_name slug -> same deterministic partner_id ->
    integrity error on the second insert).

    Normalization rules:
      - lowercase
      - drop scheme (`http://`, `https://`)
      - drop leading `www.`
      - drop trailing slash
      - drop query string + fragment
    """
    v = (url or "").strip().lower()
    if not v:
        return ""
    # Strip scheme.
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
            break
    # Strip leading www.
    if v.startswith("www."):
        v = v[4:]
    # Strip query / fragment.
    for sep in ("?", "#"):
        if sep in v:
            v = v.split(sep, 1)[0]
    # Strip trailing slash.
    return v.rstrip("/")


def _allocate_unique_partner_id(conn, *, base_partner_id: str) -> str:
    """Return `base_partner_id` if free; otherwise append `-2`,
    `-3`, ... until we find an unused slot.

    P2 audit fix: the deterministic `partner_id_for(domain, name)`
    collides when two captures share a firm + name slug (e.g. two
    `Jane Smith`s at the same fund). Pre-fix that hit a SQLite
    IntegrityError and bubbled as a 500. This function is only
    reached AFTER linkedin-url dedup has rejected the "same
    person" case, so any collision here is a genuinely different
    partner.
    """
    existing = conn.execute(
        select(partners.c.partner_id).where(
            partners.c.partner_id == base_partner_id,
        )
    ).first()
    if existing is None:
        return base_partner_id
    for n in range(2, 100):
        candidate = f"{base_partner_id}-{n}"
        hit = conn.execute(
            select(partners.c.partner_id).where(
                partners.c.partner_id == candidate,
            )
        ).first()
        if hit is None:
            return candidate
    raise HTTPException(
        500,
        f"could not allocate a unique partner_id under "
        f"{base_partner_id} after 100 attempts",
    )


@router.post(
    "/investors/capture",
    response_model=InvestorCaptureResult,
    summary=(
        "Create a partner row from a QR-scanned LinkedIn profile "
        "(or manual entry). Idempotent on linkedin_url within the "
        "workspace."
    ),
)
def capture_investor(
    body: InvestorCaptureBody,
    _auth: None = Depends(require_auth),
) -> InvestorCaptureResult:
    """FR-1 §6. Frontend uses this from the QR scanner + the
    manual-add form. Dedup contract:

      - linkedin_url already on a partner row in this workspace
        -> return that row with status='already_in_pipeline',
        DO NOT overwrite any of its fields.
      - otherwise -> create a new partner + (if needed) a new
        provisional `funds` row at `{firm-slug}.unclaimed`. Same
        DNC + is_provisional treatment as /discovery/claim for
        pseudo-domain rows so cold outreach can't accidentally
        ship before the operator fills in a real fund domain.

    Sequence seed (FR-1 §6: "newly-seeded sequence enqueued on the
    chosen cadence_key") is deferred to FR-3 when the sequences
    table lands; the cadence_key on the body is accepted now so the
    frontend doesn't have to change shape later.
    """
    channel = (body.channel or "email").strip().lower()
    if channel not in _VALID_CAPTURE_CHANNELS:
        raise HTTPException(
            422,
            f"channel must be one of {sorted(_VALID_CAPTURE_CHANNELS)}; "
            f"got {body.channel!r}",
        )
    source = (body.source or "qr").strip().lower()
    if source not in _VALID_CAPTURE_SOURCES:
        raise HTTPException(
            422,
            f"source must be one of {sorted(_VALID_CAPTURE_SOURCES)}; "
            f"got {body.source!r}",
        )

    linkedin_url_raw = (body.linkedin_url or "").strip()
    if not linkedin_url_raw:
        raise HTTPException(422, "linkedin_url is empty")
    # P2 audit fix: canonicalize the URL so dedup catches
    # formatting variants (http vs https, www, trailing slash,
    # query string). We persist the normalized form so future
    # lookups stay consistent.
    linkedin_url = _normalize_linkedin_url(linkedin_url_raw)
    if not linkedin_url:
        raise HTTPException(422, "linkedin_url normalizes to empty")
    partner_name = body.partner_name.strip()
    firm = body.firm.strip()

    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)

    with engine.begin() as conn:
        # 1) Dedup on normalized linkedin_url. The raw column may
        # hold legacy un-normalized values from pre-fix captures;
        # compare both columns for safety.
        existing_partner = conn.execute(
            select(partners.c.partner_id, partners.c.fund_id,
                   partners.c.name, partners.c.linkedin_url,
                   partners.c.channel_pref, partners.c.source)
            .where(partners.c.linkedin_url == linkedin_url)
        ).first()
        if existing_partner is None:
            # Fall back to scanning + comparing normalized URLs so
            # pre-normalization rows don't double-insert.
            for row in conn.execute(
                select(partners.c.partner_id, partners.c.fund_id,
                       partners.c.name, partners.c.linkedin_url,
                       partners.c.channel_pref, partners.c.source)
                .where(partners.c.linkedin_url.is_not(None))
            ):
                if _normalize_linkedin_url(row.linkedin_url) == linkedin_url:
                    existing_partner = row
                    break
        if existing_partner is not None:
            # Look up the firm name from the funds row.
            fund_name_row = conn.execute(
                select(funds.c.name).where(
                    funds.c.fund_id == existing_partner.fund_id,
                )
            ).first()
            return InvestorCaptureResult(
                status="already_in_pipeline",
                partner_id=existing_partner.partner_id,
                fund_id=existing_partner.fund_id,
                name=existing_partner.name,
                firm=fund_name_row.name if fund_name_row else "",
                linkedin_url=existing_partner.linkedin_url,
                channel_pref=existing_partner.channel_pref,
                source=existing_partner.source,
            )

        # 2) Create the fund row (provisional, pseudo-domain).
        # Operator-edited firms with real domains can come later.
        pseudo_domain = _slug_unclaimed_domain(firm)
        if not pseudo_domain:
            raise HTTPException(
                422, f"could not derive a domain from firm={firm!r}",
            )
        fund_id = fund_id_for(pseudo_domain)
        base_partner_id = partner_id_for(pseudo_domain, partner_name)
        # P2 audit fix: handle collisions on the deterministic
        # partner_id (same firm + name slug, different people).
        # The earlier linkedin-url dedup already returned for the
        # same-person case, so any collision here is genuinely a
        # new partner that needs a unique id.
        partner_id = _allocate_unique_partner_id(
            conn, base_partner_id=base_partner_id,
        )

        existing_fund = conn.execute(
            select(funds.c.fund_id).where(
                funds.c.fund_id == fund_id,
            )
        ).first()
        if existing_fund is None:
            conn.execute(funds.insert().values(
                fund_id=fund_id,
                name=firm,
                domain=pseudo_domain,
                is_active=True,
                is_provisional=True,
                last_updated=now,
            ))

        # 3) Create the partner row. Mark do_not_contact since the
        # firm domain is a slug; operator must fix it before
        # outreach. Same treatment as discovery_claim_pseudo_domain.
        conn.execute(partners.insert().values(
            partner_id=partner_id,
            fund_id=fund_id,
            name=partner_name,
            linkedin_url=linkedin_url,
            channel_pref=channel,
            source=source,
            is_provisional=True,
            do_not_contact=True,
            do_not_contact_reason=(
                "captured without a real fund domain "
                f"({pseudo_domain}); edit the fund's domain "
                "before contacting"
            ),
            do_not_contact_source="capture_pseudo_domain",
            do_not_contact_set_at=now,
            bio=body.note,  # operator's QR-time note lands as bio
            last_updated=now,
        ))

        # FR-3: seed the sequence row so the daily build loop has
        # something to advance. body.cadence_key is currently
        # informational (FR-3 only tracks ONE active sequence per
        # partner regardless of warm/cold path; the per-key cadence
        # config is FR-7's parallel-channel work).
        from web.routers.sequences import seed_sequence_for_partner
        seed_sequence_for_partner(conn, partner_id=partner_id)

    return InvestorCaptureResult(
        status="created",
        partner_id=partner_id,
        fund_id=fund_id,
        name=partner_name,
        firm=firm,
        linkedin_url=linkedin_url,
        channel_pref=channel,
        source=source,
        note=body.note,
    )


# ---------- FR-7 mark-sent + clear-sent (manual-paste channels) ----------

_VALID_MARK_SENT_CHANNELS = {"email", "linkedin"}


class MarkSentBody(BaseModel):
    channel: str = Field(
        default="linkedin",
        description=(
            "Channel the operator used to send. 'linkedin' is the "
            "primary use case (operator pasted the draft into a "
            "LinkedIn DM and sent). 'email' is here for parity "
            "with the same UI button when the operator sent from "
            "an email client outside Gmail Drafts. Gmail-confirmed "
            "sends do NOT use this endpoint -- the gmail-sent "
            "poller writes those automatically."
        ),
    )
    note: str | None = Field(
        default=None, max_length=500,
        description="Optional operator-side audit note.",
    )


class MarkSentView(BaseModel):
    draft_id: int
    channel: str
    event_id: int
    sent_at: str


@router.post(
    "/drafts/{draft_id}/mark-sent",
    response_model=MarkSentView,
    summary=(
        "Mark a draft as sent via a manual-paste channel "
        "(LinkedIn, off-platform email, etc.)"
    ),
)
def mark_draft_sent(
    draft_id: int,
    body: MarkSentBody | None = None,
    _auth: None = Depends(require_auth),
) -> MarkSentView:
    """FR-7. Operator pasted the draft into LinkedIn (or another
    channel that isn't Gmail) and sent it. Flip the draft's
    approval_status to 'sent' AND log an `outreach_events` row
    with source='app' + channel=<channel> so the audit trail
    distinguishes manual sends from Gmail-poll-detected ones.

    Idempotent on re-call.
    """
    from core.approval.persistence import mark_sent as _mark_sent
    from core.db import outreach_events
    body = body or MarkSentBody()
    channel = (body.channel or "linkedin").strip().lower()
    if channel not in _VALID_MARK_SENT_CHANNELS:
        raise HTTPException(
            422,
            f"channel must be one of {sorted(_VALID_MARK_SENT_CHANNELS)}; "
            f"got {body.channel!r}",
        )
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        draft_row = conn.execute(
            select(
                email_drafts.c.draft_id,
                email_drafts.c.partner_id,
                email_drafts.c.approval_status,
                email_drafts.c.subject,
                email_drafts.c.body,
            ).where(email_drafts.c.draft_id == draft_id)
        ).first()
        if draft_row is None:
            raise HTTPException(
                404, f"unknown draft_id: {draft_id}",
            )
        partner_id = draft_row.partner_id

        existing_event = conn.execute(
            select(
                outreach_events.c.event_id,
                outreach_events.c.channel,
                outreach_events.c.occurred_at,
            ).where(
                outreach_events.c.source == "app",
                outreach_events.c.event_type == "sent",
                outreach_events.c.draft_id == draft_id,
            ).order_by(desc(outreach_events.c.occurred_at))
        ).first()
        if existing_event is not None:
            return MarkSentView(
                draft_id=draft_id,
                channel=existing_event.channel or "linkedin",
                event_id=int(existing_event.event_id),
                sent_at=(
                    existing_event.occurred_at.isoformat()
                    if existing_event.occurred_at else now.isoformat()
                ),
            )

        result = conn.execute(outreach_events.insert().values(
            source="app",
            event_type="sent",
            partner_id=partner_id,
            draft_id=draft_id,
            channel=channel,
            subject=draft_row.subject,
            body_snippet=(
                (draft_row.body or "")[:200] if draft_row.body else None
            ),
            occurred_at=now,
            unread=False,
            created_at=now,
        ))
        event_id = int(result.inserted_primary_key[0])

    from core.approval.state_machine import STATE_SENT
    from core.db import draft_approvals
    current_status = draft_row.approval_status
    if current_status != STATE_SENT:
        if current_status == "approved_to_send":
            _mark_sent(
                engine, draft_id=draft_id, partner_id=partner_id,
                actor="ui:mark_sent", notes=f"channel={channel}",
            )
        else:
            # Audit-review fix #9: stamp the state directly AND
            # write a draft_approvals audit row. Pre-fix, the
            # bypass for non-approved starting states (needs_review,
            # rejected, stale_after_approval) skipped the audit
            # write entirely -- the 'sent' transition vanished from
            # draft_approvals.list_events(draft_id), breaking
            # provenance.
            with engine.begin() as conn:
                draft_hash = conn.execute(
                    select(email_drafts.c.draft_hash).where(
                        email_drafts.c.draft_id == draft_id,
                    )
                ).scalar()
                conn.execute(draft_approvals.insert().values(
                    draft_id=draft_id,
                    partner_id=partner_id,
                    event_type=STATE_SENT,
                    actor="ui:mark_sent",
                    at=now,
                    draft_hash=draft_hash,
                    notes=(
                        f"channel={channel}; "
                        f"bypassed_state_machine_from={current_status}"
                    ),
                    overridden_blockers=None,
                ))
                conn.execute(
                    email_drafts.update()
                    .where(email_drafts.c.draft_id == draft_id)
                    .values(approval_status=STATE_SENT)
                )
    return MarkSentView(
        draft_id=draft_id,
        channel=channel,
        event_id=event_id,
        sent_at=now.isoformat(),
    )


@router.delete(
    "/drafts/{draft_id}/mark-sent",
    response_model=dict,
    summary=(
        "Revert a manual mark-sent (operator mis-clicked, or "
        "the LinkedIn DM didn't actually go through)"
    ),
)
def clear_draft_sent(
    draft_id: int,
    _auth: None = Depends(require_auth),
) -> dict:
    """FR-7. Reverses POST /drafts/{id}/mark-sent. Only removes
    APP-sourced sent events (gmail-poll-confirmed sends are
    terminal and not reversible from the UI). Flips
    approval_status back to 'approved_to_send' so the draft
    re-appears in the queue.

    Bypasses the state-machine transition table since STATE_SENT
    has no outbound edges by design (sent-is-terminal invariant).
    Still writes a draft_approvals audit row so the reverse
    action shows up in the trail.
    """
    from core.db import draft_approvals, outreach_events
    from core.approval.state_machine import (
        STATE_APPROVED_TO_SEND, STATE_SENT,
    )
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        draft_row = conn.execute(
            select(
                email_drafts.c.draft_id,
                email_drafts.c.partner_id,
                email_drafts.c.approval_status,
                email_drafts.c.draft_hash,
            ).where(email_drafts.c.draft_id == draft_id)
        ).first()
        if draft_row is None:
            raise HTTPException(
                404, f"unknown draft_id: {draft_id}",
            )
        event = conn.execute(
            select(outreach_events.c.event_id).where(
                outreach_events.c.source == "app",
                outreach_events.c.event_type == "sent",
                outreach_events.c.draft_id == draft_id,
            ).order_by(desc(outreach_events.c.occurred_at))
        ).first()
        if event is None:
            raise HTTPException(
                404,
                f"no manual mark-sent event on file for "
                f"draft_id={draft_id} (gmail-poll-confirmed sends "
                f"are not reversible from this endpoint)",
            )
        conn.execute(
            outreach_events.delete().where(
                outreach_events.c.event_id == event.event_id,
            )
        )
        if draft_row.approval_status == STATE_SENT:
            # Audit-review fix #10: record the AT-SEND hash, not
            # the current email_drafts.draft_hash. If the body was
            # edited between mark-sent and clear-sent, the current
            # hash differs from the body that actually went out.
            # The most-recent STATE_SENT draft_approvals row (or
            # equivalently the mark_sent transition recorded by
            # core.approval.persistence.mark_sent) carries the
            # at-send hash; fall back to the current hash only if
            # no sent-event audit row exists (shouldn't happen
            # post-#9 fix but keeps the path graceful).
            sent_audit = conn.execute(
                select(draft_approvals.c.draft_hash).where(
                    draft_approvals.c.draft_id == draft_id,
                    draft_approvals.c.event_type == STATE_SENT,
                ).order_by(desc(draft_approvals.c.at))
            ).first()
            at_send_hash = (
                sent_audit.draft_hash if sent_audit
                else draft_row.draft_hash
            )
            conn.execute(draft_approvals.insert().values(
                draft_id=draft_id,
                partner_id=draft_row.partner_id,
                event_type=STATE_APPROVED_TO_SEND,
                actor="ui:clear_sent",
                at=_dt.datetime.now(_dt.timezone.utc),
                draft_hash=at_send_hash,
                notes="operator reverted manual mark-sent (FR-7)",
                overridden_blockers=None,
            ))
            conn.execute(
                email_drafts.update()
                .where(email_drafts.c.draft_id == draft_id)
                .values(approval_status=STATE_APPROVED_TO_SEND)
            )
    return {
        "draft_id": draft_id,
        "reverted_event_id": int(event.event_id),
    }
