"""Investor-management router (FR-1 frontend wins).

  PUT /investors/{partner_id}/status   — manual pipeline-stage
                                          override. Alias for the
                                          coach router's POST
                                          /partners/{id}/pipeline
                                          so the frontend's
                                          existing setStatus()
                                          call site works.
  PUT /investors/{partner_id}/channel  — set channel_pref
                                          ('email' | 'linkedin' |
                                          'both'). Defaults to
                                          'email' if never set.
  POST /drafts/{draft_id}/snooze       — alias for POST
                                          /snoozes/{draft_id};
                                          frontend uses both
                                          shapes.

Per FR-1 §10: pipeline status override policy is "local-only for
v1". When a CRM is connected the push-through to the CRM happens
inside the existing partner_pipeline write path; the operator's
local override sits in the same table.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.db import draft_snoozes, email_drafts, partner_pipeline, partners, upsert
from web.deps import CommandResult, _engine_and_ws, require_auth
from web.routers.coach import (
    SnoozeBody, SnoozeView, _parse_future_iso, set_snooze,
)


_VALID_CHANNELS = {"email", "linkedin", "both"}


class InvestorStatusBody(BaseModel):
    status: str = Field(
        min_length=1, max_length=64,
        description=(
            "Pipeline stage / status. Free-form string; "
            "conventional values: 'contacted', 'meeting_set', "
            "'passed', 'invested', etc. Matches the values "
            "accepted by POST /partners/{id}/pipeline.stage."
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
    channel_pref: str  # always set; defaults to 'email' if never written


router = APIRouter(tags=["investors"])


@router.put(
    "/investors/{partner_id}/status",
    response_model=InvestorStatusView,
    summary=(
        "Set the partner's pipeline status (alias for POST "
        "/partners/{id}/pipeline.stage)"
    ),
)
def set_investor_status(
    partner_id: str,
    body: InvestorStatusBody,
    _auth: None = Depends(require_auth),
) -> InvestorStatusView:
    """FR-1 §10. Writes the override into `partner_pipeline.stage`
    -- same table B4's POST /partners/{id}/pipeline writes -- so
    Today / review surfaces see the change immediately.

    Policy (FR-1 §10 option C): local-only for v1. Push-through to
    the connected CRM is deferred until we wire OutboundAttioClient.
    """
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
    """FR-1 §8. Persists `partners.channel_pref`. Returns 422 on
    invalid values (the whitelist is server-side; frontend can
    only render the three valid options anyway, but a stale
    cached client could send something else)."""
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


@router.post(
    "/drafts/{draft_id}/snooze",
    response_model=SnoozeView,
    summary=(
        "Snooze a draft (alias for POST /snoozes/{draft_id}; "
        "frontend uses both URL shapes)"
    ),
)
def snooze_draft_alias(
    draft_id: int,
    body: SnoozeBody | None = None,
    _auth: None = Depends(require_auth),
) -> SnoozeView:
    """FR-1 §9. Frontend's mockApi.setSnooze() targets
    /drafts/{id}/snooze rather than /snoozes/{id}. Behaviourally
    identical to the coach router's endpoint, plus an `until: null`
    convenience for unsnooze in the same call site.
    """
    if body is None or not body.snoozed_until:
        # Unsnooze shortcut: { "until": null }.
        engine, _ = _engine_and_ws()
        with engine.begin() as conn:
            conn.execute(
                draft_snoozes.delete().where(
                    draft_snoozes.c.draft_id == draft_id,
                )
            )
        return SnoozeView(draft_id=draft_id)
    return set_snooze(draft_id=draft_id, body=body, _auth=None)
