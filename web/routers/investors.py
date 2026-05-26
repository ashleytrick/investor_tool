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
from sqlalchemy import select

from core.db import (
    draft_snoozes, email_drafts, partner_pipeline, partners, upsert,
)
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
