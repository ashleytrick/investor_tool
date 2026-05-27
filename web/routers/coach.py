"""Coach router (refactor #16 — final extraction).

The operator-facing daily-work surface. All 10 endpoints below
share the same per-user workspace routing + JWT auth contract;
they're grouped here because they're what the Lovable Coach UI
calls every minute the operator is in the app.

  GET    /today                         B1 — daily ranked batch
  GET    /settings/send-pace            B1 — read pace setting
  POST   /settings/send-pace            B1 — update pace
  GET    /settings/discovery-opt-in     Review #11 — read opt-in
  POST   /settings/discovery-opt-in     Review #11 — flip opt-in
  GET    /partners/{id}/pipeline        B4 — read pipeline stage
  POST   /partners/{id}/pipeline        B4 — update stage
  GET    /snoozes/{draft_id}            B4 — read snooze
  POST   /snoozes/{draft_id}            B4 — set snooze
  DELETE /snoozes/{draft_id}            B4 — clear snooze
  GET    /sent                          B2 — Gmail Sent events
  GET    /replies                       B3 — Gmail reply events
  POST   /replies/{event_id}/read       B3 — mark reply read

Shared shapes (DraftView, GateInfo, CommandResult) and helpers
(serialize_draft, gate_to_dict, rationale_by_partner) live in
web/deps.py so /review/pending in api.py can keep using them
without an import cycle through this module.

Paths + behavior byte-identical to pre-extraction.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from core.approval.gate import can_approve_draft
from core.approval.persistence import REVIEWABLE_STATES
from core.db import (
    draft_snoozes,
    email_drafts,
    partner_pipeline,
    partner_score_summaries,
    partners,
    today_picks,
    upsert,
    workspace_settings,
)
from web.deps import (
    CommandResult,
    DraftView,
    _allow_example_domains_args,
    _engine_and_ws,
    gate_to_dict,
    rationale_by_partner,
    require_auth,
    serialize_draft,
)


# ---------- B1 send-pace setting ----------

_SEND_PACE_KEY = "send_pace"
_SEND_PACE_DEFAULT = 10
_SEND_PACE_MIN = 1
_SEND_PACE_MAX = 20


def _read_send_pace(conn: Any) -> int:
    row = conn.execute(
        select(workspace_settings.c.value)
        .where(workspace_settings.c.key == _SEND_PACE_KEY)
    ).first()
    if not row or not row.value:
        return _SEND_PACE_DEFAULT
    try:
        n = int(row.value)
    except (TypeError, ValueError):
        return _SEND_PACE_DEFAULT
    return max(_SEND_PACE_MIN, min(_SEND_PACE_MAX, n))


def _write_send_pace(conn: Any, value: int) -> int:
    clamped = max(_SEND_PACE_MIN, min(_SEND_PACE_MAX, value))
    upsert(
        conn, workspace_settings, ["key"],
        {
            "key": _SEND_PACE_KEY,
            "value": str(clamped),
            "updated_at": _dt.datetime.now(_dt.timezone.utc),
        },
    )
    return clamped


# ---------- review #11 discovery-pool opt-in ----------

_DISCOVERY_OPT_IN_KEY = "investors_global_opted_in"


def _read_discovery_opt_in(conn: Any) -> bool:
    row = conn.execute(
        select(workspace_settings.c.value)
        .where(workspace_settings.c.key == _DISCOVERY_OPT_IN_KEY)
    ).first()
    if not row or not row.value:
        return False
    return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


def _write_discovery_opt_in(conn: Any, opted_in: bool) -> bool:
    upsert(
        conn, workspace_settings, ["key"],
        {
            "key": _DISCOVERY_OPT_IN_KEY,
            "value": "true" if opted_in else "false",
            "updated_at": _dt.datetime.now(_dt.timezone.utc),
        },
    )
    return opted_in


# ---------- schemas ----------

class FollowUpContext(BaseModel):
    """FR-4: hydrated context for a follow-up touch (touch 2..N).
    Initial outreach (touch 1) draws from email_drafts; follow-ups
    draw from follow_up_drafts + sequences. The frontend
    distinguishes the two via `follow_up != None` -- the renderer
    shows the thread preview, days-since, and angle on follow-up
    cards."""
    touch_number: int  # 2..N
    max_touches: int
    days_since_last_touch: int
    angle: str  # new_signal | specific_ask | soft_check_in | graceful_close | custom
    why_now: str | None = None
    thread_preview: str | None = None
    thread_sent_at: str | None = None  # ISO datetime
    sequence_id: str


class TodayPickView(BaseModel):
    """One pick on the daily Today queue. After FR-4 the same
    shape carries both initial outreach (`follow_up is None`) and
    follow-up touches (`follow_up` populated)."""
    pick_date: str  # ISO date (YYYY-MM-DD)
    rank: int
    partner_id: str
    draft_id: int
    rationale: str | None = None
    # FR-4: most-recent snooze on file; None if never snoozed.
    # Active snoozes still filter the pick out entirely; this
    # field is for UI history hints ("previously snoozed until X").
    snoozed_until: str | None = None
    # FR-4: populated for follow-up touches; None for initial
    # outreach. See FollowUpContext.
    follow_up: FollowUpContext | None = None
    draft: DraftView | None = None


class TodayResponse(BaseModel):
    """FR-4 envelope wrapping today's picks. Replaces the legacy
    `list[TodayPickView]` shape so the frontend can render the
    daily batch + a "next up" preview + a remaining-count badge
    without round-tripping."""
    date: str  # ISO date YYYY-MM-DD
    send_pace: int  # operator's configured daily pace
    drafts: list[TodayPickView]  # the batch to work on now (<= effective_limit)
    next_drafts: list[TodayPickView]  # preview of the next batch
    total_remaining: int  # eligible drafts not yet shown in `drafts`


class SendPaceBody(BaseModel):
    value: int = Field(
        ge=_SEND_PACE_MIN, le=_SEND_PACE_MAX,
        description=(
            f"Drafts-per-day pace. Clamped server-side to "
            f"[{_SEND_PACE_MIN}, {_SEND_PACE_MAX}]."
        ),
    )


class SendPaceView(BaseModel):
    value: int


class DiscoveryOptInView(BaseModel):
    """Per-tenant opt-in for the shared `investors_global`
    discovery pool. Default False; operator-level
    `INVESTORS_GLOBAL_DISABLED=true` env var overrides this."""
    opted_in: bool


class DiscoveryOptInBody(BaseModel):
    opted_in: bool


class PipelineView(BaseModel):
    partner_id: str
    stage: str | None = None
    notes: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


class PipelineBody(BaseModel):
    stage: str = Field(min_length=1, max_length=64)
    notes: str | None = None


class SnoozeView(BaseModel):
    draft_id: int
    snoozed_until: str | None = None
    reason: str | None = None
    created_at: str | None = None


class SnoozeBody(BaseModel):
    snoozed_until: str = Field(
        description=(
            "ISO datetime in the future. Server compares against "
            "now(UTC); past values are rejected."
        ),
    )
    reason: str | None = None


class SentItem(BaseModel):
    """One row on the Coach Sent tab."""
    event_id: int
    partner_id: str | None = None
    partner_name: str | None = None
    partner_email: str | None = None
    draft_id: int | None = None
    external_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    body_snippet: str | None = None
    recipient_email: str | None = None
    occurred_at: str


class ReplyItem(BaseModel):
    """One row on the Coach Replies tab."""
    event_id: int
    partner_id: str | None = None
    partner_name: str | None = None
    partner_email: str | None = None
    draft_id: int | None = None
    external_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    body_snippet: str | None = None
    sender_email: str | None = None
    occurred_at: str
    classification: str | None = None
    unread: bool = False


# ---------- snooze validation ----------

def _parse_future_iso(value: str):
    """Parse an ISO datetime and require it's in the future.

    Returns a tz-NAIVE UTC datetime so writers + readers agree
    explicitly. (SQLAlchemy strips tzinfo on SQLite anyway, but
    the symmetry helps future Postgres deploys.)
    """
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            422, f"snoozed_until must be ISO 8601: {exc}",
        )
    if dt.tzinfo is None:
        utc_aware = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        utc_aware = dt.astimezone(_dt.timezone.utc)
    if utc_aware <= _dt.datetime.now(_dt.timezone.utc):
        raise HTTPException(
            422, "snoozed_until must be in the future",
        )
    return utc_aware.replace(tzinfo=None)


router = APIRouter(tags=["coach"])


# ---------- B1 Today + settings ----------

@router.get(
    "/today",
    response_model=TodayResponse,
    summary="Today's ranked draft batch (FR-4 envelope, stable per day)",
)
def get_today(
    limit: int | None = Query(
        None, ge=1, le=_SEND_PACE_MAX,
        description=(
            "Optional batch size for `drafts`. Defaults to the "
            "workspace's `send_pace` setting (1..20)."
        ),
    ),
    _auth: None = Depends(require_auth),
) -> TodayResponse:
    """FR-4 envelope: `{date, send_pace, drafts, next_drafts,
    total_remaining}`. `drafts` is the batch to work on now,
    `next_drafts` previews what's coming, `total_remaining`
    counts eligible partners not yet in `drafts` so a "X more
    in the pipeline" badge stays honest.

    Materialization aims for `effective_limit + send_pace` rows
    so headroom is reserved for the preview without a second
    query. Follow-up touches (touch 2+) will populate the
    per-pick `follow_up` field once FR-5's daily build job lands.
    """
    engine, ws = _engine_and_ws()
    today_iso = _dt.date.today()

    with engine.begin() as conn:
        send_pace = _read_send_pace(conn)
        effective_limit = limit if limit is not None else send_pace
        target_materialize = effective_limit + send_pace

        now_dt = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        active_snooze_ids = {
            row.draft_id for row in conn.execute(
                select(draft_snoozes.c.draft_id)
                .where(draft_snoozes.c.snoozed_until > now_dt)
            )
        }

        existing = [
            r for r in conn.execute(
                select(today_picks)
                .where(today_picks.c.pick_date == today_iso)
                .order_by(today_picks.c.rank)
            )
            if r.draft_id not in active_snooze_ids
        ]
        if existing:
            picks_rows = existing[:target_materialize]
        else:
            snoozed_subq = (
                select(draft_snoozes.c.draft_id)
                .where(draft_snoozes.c.snoozed_until > now_dt)
            )
            raw_candidates = list(conn.execute(
                select(
                    email_drafts.c.draft_id,
                    email_drafts.c.partner_id,
                    partner_score_summaries.c.send_now_priority,
                    partner_score_summaries.c.recommendation_reasoning,
                )
                .join(
                    partner_score_summaries,
                    partner_score_summaries.c.partner_id
                    == email_drafts.c.partner_id,
                    isouter=True,
                )
                .where(
                    email_drafts.c.approval_status.in_(
                        list(REVIEWABLE_STATES)
                    ),
                    email_drafts.c.superseded_at.is_(None),
                    ~email_drafts.c.draft_id.in_(snoozed_subq),
                )
                .order_by(
                    desc(partner_score_summaries.c.send_now_priority),
                    email_drafts.c.draft_id,
                )
            ))
            seen: set[str] = set()
            candidates: list[Any] = []
            for row in raw_candidates:
                if row.partner_id in seen:
                    continue
                seen.add(row.partner_id)
                candidates.append(row)
                if len(candidates) >= target_materialize:
                    break
            now = _dt.datetime.now(_dt.timezone.utc)
            for rank, row in enumerate(candidates, start=1):
                upsert(
                    conn, today_picks, ["pick_date", "partner_id"],
                    {
                        "pick_date": today_iso,
                        "partner_id": row.partner_id,
                        "draft_id": int(row.draft_id),
                        "rank": rank,
                        "rationale": row.recommendation_reasoning,
                        "created_at": now,
                    },
                )
            picks_rows = [
                r for r in conn.execute(
                    select(today_picks)
                    .where(today_picks.c.pick_date == today_iso)
                    .order_by(today_picks.c.rank)
                )
                if r.draft_id not in active_snooze_ids
            ][:target_materialize]

        # FR-4: total_remaining counts distinct eligible partners
        # (reviewable, not superseded, not currently snoozed) so
        # the frontend can show "X more in the pipeline". Fresh
        # on every call -- true pool size, not snapshot.
        snoozed_subq_count = (
            select(draft_snoozes.c.draft_id)
            .where(draft_snoozes.c.snoozed_until > now_dt)
        )
        total_eligible_partners = conn.execute(
            select(func.count(func.distinct(email_drafts.c.partner_id)))
            .where(
                email_drafts.c.approval_status.in_(
                    list(REVIEWABLE_STATES)
                ),
                email_drafts.c.superseded_at.is_(None),
                ~email_drafts.c.draft_id.in_(snoozed_subq_count),
            )
        ).scalar() or 0

        if not picks_rows:
            return TodayResponse(
                date=today_iso.isoformat(),
                send_pace=send_pace,
                drafts=[],
                next_drafts=[],
                total_remaining=int(total_eligible_partners),
            )

        partner_ids = [p.partner_id for p in picks_rows]
        draft_ids = [int(p.draft_id) for p in picks_rows if p.draft_id]
        drafts_by_id: dict[int, Any] = {
            row.draft_id: row
            for row in conn.execute(
                select(email_drafts).where(
                    email_drafts.c.draft_id.in_(draft_ids)
                )
            )
        }
        email_by_pid = {
            r.partner_id: r.email or ""
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email)
                .where(partners.c.partner_id.in_(partner_ids))
            )
        }
        # FR-4: hydrate the most-recent snooze (even if elapsed)
        # so the UI can render "previously snoozed until X" hints.
        # Active snoozes are already filtered out of picks_rows.
        snoozes_by_draft = {
            r.draft_id: r.snoozed_until
            for r in conn.execute(
                select(
                    draft_snoozes.c.draft_id,
                    draft_snoozes.c.snoozed_until,
                ).where(draft_snoozes.c.draft_id.in_(draft_ids))
            )
        }

    all_picks: list[TodayPickView] = []
    for p in picks_rows:
        d = drafts_by_id.get(int(p.draft_id)) if p.draft_id else None
        draft_view: DraftView | None = None
        if d is not None:
            gate = can_approve_draft(
                ws, engine, int(d.draft_id),
                allow_example_domains=bool(_allow_example_domains_args()),
            )
            draft_view = serialize_draft(
                d,
                partner_email=email_by_pid.get(d.partner_id),
                gate=gate_to_dict(gate),
                rationale=p.rationale,
            )
        snoozed_until_ts = (
            snoozes_by_draft.get(int(p.draft_id)) if p.draft_id else None
        )
        # Pass `draft` as a dict (not the DraftView instance) so
        # Pydantic doesn't trip on class-identity checks when this
        # module gets reloaded in tests via importlib.reload.
        all_picks.append(TodayPickView(
            pick_date=str(p.pick_date),
            rank=int(p.rank),
            partner_id=str(p.partner_id),
            draft_id=int(p.draft_id) if p.draft_id else 0,
            rationale=p.rationale,
            snoozed_until=(
                snoozed_until_ts.isoformat()
                if snoozed_until_ts else None
            ),
            # FR-4: touch 1 has no follow-up context. FR-5 will
            # populate this for touch 2+ entries.
            follow_up=None,
            draft=(draft_view.model_dump() if draft_view else None),
        ))

    drafts_now = all_picks[:effective_limit]
    drafts_next = all_picks[effective_limit:effective_limit + send_pace]
    total_remaining = max(
        0, int(total_eligible_partners) - len(drafts_now),
    )
    return TodayResponse(
        date=today_iso.isoformat(),
        send_pace=send_pace,
        drafts=drafts_now,
        next_drafts=drafts_next,
        total_remaining=total_remaining,
    )


@router.get(
    "/settings/send-pace",
    response_model=SendPaceView,
    summary="Read the workspace's daily send-pace setting",
)
def get_send_pace(_auth: None = Depends(require_auth)) -> SendPaceView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        return SendPaceView(value=_read_send_pace(conn))


@router.post(
    "/settings/send-pace",
    response_model=SendPaceView,
    summary="Update the workspace's daily send-pace setting (clamped 1-20)",
)
def set_send_pace(
    body: SendPaceBody,
    _auth: None = Depends(require_auth),
) -> SendPaceView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        return SendPaceView(value=_write_send_pace(conn, body.value))


# ---------- review #11 discovery-pool opt-in ----------

@router.get(
    "/settings/discovery-opt-in",
    response_model=DiscoveryOptInView,
    summary="Read the tenant's discovery-pool opt-in flag",
    tags=["onboarding"],
)
def get_discovery_opt_in(
    _auth: None = Depends(require_auth),
) -> DiscoveryOptInView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        return DiscoveryOptInView(
            opted_in=_read_discovery_opt_in(conn),
        )


@router.post(
    "/settings/discovery-opt-in",
    response_model=DiscoveryOptInView,
    summary=(
        "Set the tenant's discovery-pool opt-in flag (frontend "
        "prompts during onboarding)"
    ),
    tags=["onboarding"],
)
def set_discovery_opt_in(
    body: DiscoveryOptInBody,
    _auth: None = Depends(require_auth),
) -> DiscoveryOptInView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        return DiscoveryOptInView(
            opted_in=_write_discovery_opt_in(conn, body.opted_in),
        )


# ---------- B4 pipeline + snoozes ----------

@router.get(
    "/partners/{partner_id}/pipeline",
    response_model=PipelineView,
    summary="Get the partner's pipeline stage + notes",
)
def get_pipeline(
    partner_id: str,
    _auth: None = Depends(require_auth),
) -> PipelineView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        row = conn.execute(
            select(partner_pipeline).where(
                partner_pipeline.c.partner_id == partner_id,
            )
        ).first()
    if row is None:
        return PipelineView(partner_id=partner_id)
    return PipelineView(
        partner_id=row.partner_id,
        stage=row.stage,
        notes=row.notes,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        updated_by=row.updated_by,
    )


@router.post(
    "/partners/{partner_id}/pipeline",
    response_model=PipelineView,
    summary="Set the partner's pipeline stage (and optional notes)",
)
def set_pipeline(
    partner_id: str,
    body: PipelineBody,
    _auth: None = Depends(require_auth),
) -> PipelineView:
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(partners.c.partner_id).where(
                partners.c.partner_id == partner_id,
            )
        ).first()
        if partner_row is None:
            raise HTTPException(404, f"unknown partner_id: {partner_id}")
        upsert(
            conn, partner_pipeline, ["partner_id"],
            {
                "partner_id": partner_id,
                "stage": body.stage,
                "notes": body.notes,
                "updated_at": now,
                "updated_by": None,  # TODO: stamp from principal
            },
        )
    return PipelineView(
        partner_id=partner_id,
        stage=body.stage,
        notes=body.notes,
        updated_at=now.isoformat(),
    )


@router.get(
    "/snoozes/{draft_id}",
    response_model=SnoozeView,
    summary="Get the snooze for a draft (or empty view if not snoozed)",
)
def get_snooze(
    draft_id: int,
    _auth: None = Depends(require_auth),
) -> SnoozeView:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        row = conn.execute(
            select(draft_snoozes).where(
                draft_snoozes.c.draft_id == draft_id,
            )
        ).first()
    if row is None:
        return SnoozeView(draft_id=draft_id)
    return SnoozeView(
        draft_id=row.draft_id,
        snoozed_until=(
            row.snoozed_until.isoformat() if row.snoozed_until else None
        ),
        reason=row.reason,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


@router.post(
    "/snoozes/{draft_id}",
    response_model=SnoozeView,
    summary="Snooze a draft until the specified ISO datetime",
)
def set_snooze(
    draft_id: int,
    body: SnoozeBody,
    _auth: None = Depends(require_auth),
) -> SnoozeView:
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    snoozed_until = _parse_future_iso(body.snoozed_until)
    with engine.begin() as conn:
        draft_row = conn.execute(
            select(email_drafts.c.draft_id).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).first()
        if draft_row is None:
            raise HTTPException(404, f"unknown draft_id: {draft_id}")
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
    return SnoozeView(
        draft_id=draft_id,
        snoozed_until=snoozed_until.isoformat(),
        reason=body.reason,
        created_at=now.isoformat(),
    )


@router.delete(
    "/snoozes/{draft_id}",
    response_model=CommandResult,
    summary="Clear a snooze (so the draft becomes eligible again)",
)
def clear_snooze(
    draft_id: int,
    _auth: None = Depends(require_auth),
) -> CommandResult:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        res = conn.execute(
            draft_snoozes.delete().where(
                draft_snoozes.c.draft_id == draft_id,
            )
        )
    if (res.rowcount or 0) == 0:
        raise HTTPException(
            404, f"no snooze on file for draft_id={draft_id}",
        )
    return CommandResult(ok=True, stdout="snooze cleared")


# ---------- B2 Sent + B3 Replies read paths ----------

@router.get(
    "/sent",
    response_model=list[SentItem],
    summary="Recent Gmail Sent events for the current tenant",
)
def get_sent(
    limit: int = Query(default=100, ge=1, le=500),
    _auth: None = Depends(require_auth),
) -> list[SentItem]:
    from core.outreach_events import list_sent_events
    engine, _ = _engine_and_ws()
    rows = list_sent_events(engine, limit=limit)
    out: list[SentItem] = []
    for r in rows:
        occurred = r["occurred_at"]
        occurred_iso = (
            occurred.isoformat()
            if hasattr(occurred, "isoformat") else str(occurred)
        )
        out.append(SentItem(
            event_id=int(r["event_id"]),
            partner_id=r.get("partner_id"),
            partner_name=r.get("partner_name"),
            partner_email=r.get("partner_email"),
            draft_id=(
                int(r["draft_id"]) if r.get("draft_id") is not None
                else None
            ),
            external_id=r.get("external_id"),
            thread_id=r.get("thread_id"),
            subject=r.get("subject"),
            body_snippet=r.get("body_snippet"),
            recipient_email=r.get("recipient_email"),
            occurred_at=occurred_iso,
        ))
    return out


@router.get(
    "/replies",
    response_model=list[ReplyItem],
    summary="Recent Gmail reply events for the current tenant",
)
def get_replies(
    limit: int = Query(default=100, ge=1, le=500),
    unread_only: bool = Query(
        default=False,
        description="Filter to `unread=true` rows (inbox view)",
    ),
    _auth: None = Depends(require_auth),
) -> list[ReplyItem]:
    from core.outreach_events import list_reply_events
    engine, _ = _engine_and_ws()
    rows = list_reply_events(engine, limit=limit, unread_only=unread_only)
    out: list[ReplyItem] = []
    for r in rows:
        occurred = r["occurred_at"]
        occurred_iso = (
            occurred.isoformat()
            if hasattr(occurred, "isoformat") else str(occurred)
        )
        out.append(ReplyItem(
            event_id=int(r["event_id"]),
            partner_id=r.get("partner_id"),
            partner_name=r.get("partner_name"),
            partner_email=r.get("partner_email"),
            draft_id=(
                int(r["draft_id"]) if r.get("draft_id") is not None
                else None
            ),
            external_id=r.get("external_id"),
            thread_id=r.get("thread_id"),
            subject=r.get("subject"),
            body_snippet=r.get("body_snippet"),
            sender_email=r.get("recipient_email"),
            occurred_at=occurred_iso,
            classification=r.get("classification"),
            unread=bool(r.get("unread", False)),
        ))
    return out


@router.post(
    "/replies/{event_id}/read",
    response_model=CommandResult,
    summary="Mark a reply event as read",
)
def mark_reply_as_read(
    event_id: int,
    _auth: None = Depends(require_auth),
) -> CommandResult:
    from core.outreach_events import mark_reply_read
    engine, _ = _engine_and_ws()
    updated = mark_reply_read(engine, event_id=event_id)
    if not updated:
        raise HTTPException(
            404, "no unread reply with that event_id in this workspace",
        )
    return CommandResult(ok=True, stdout="marked read")
