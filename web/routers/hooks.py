"""Cron-hook router (refactor item #16).

All 10 cron-triggered polling/build hooks live here:

  POST /api/public/hooks/poll-gmail-sent       (B2, 10min)
  POST /api/public/hooks/poll-gmail-replies    (B3, 10min)
  POST /api/public/hooks/reconcile-drafts      (B3, 30min)
  POST /api/public/hooks/build-follow-ups      (FR-5, 1x/day @ 06:00)
  POST /api/public/hooks/poll-crm-activity     (B6, 15min)
  POST /api/public/hooks/poll-crm-pipeline     (B6, 30min)
  POST /api/public/hooks/poll-crm-investors    (B7, 6h)
  POST /api/public/hooks/poll-crm-relationships (B7, 6h)
  POST /api/public/hooks/poll-crm-lists        (B8, 1h)
  POST /api/public/hooks/poll-crm-deals        (B8, 1h)

Auth model: shared `X-Hook-Secret` header == `HOOK_SECRET` env
var. NOT JWT -- the caller is infrastructure (Fly cron / external
scheduler), not a user. Fail-closed when `HOOK_SECRET` is unset
(500), so an accidentally-empty env var doesn't open the hooks
to anonymous polling.

Behavior of all nine endpoints follows the same shape:
  - scatter-gather across every per-user workspace (or the
    pinned legacy workspace if WORKSPACE_PER_USER is off)
  - per-tenant / per-provider errors land in results[].error
    so one bad workspace doesn't tank the rest
  - response body is { polled, total_inserted, results: [...] }

Paths + behavior are byte-identical to the pre-extraction
versions in web/api.py.
"""
from __future__ import annotations

import hmac
import os
import pathlib

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from web.deps import _ws_path


# ---------- shared auth ----------

def _hook_secret_required(
    x_hook_secret: str | None = Header(default=None, alias="X-Hook-Secret"),
) -> None:
    """Cron-hook auth: shared secret in X-Hook-Secret. Without
    `HOOK_SECRET` set in env, every request is refused (fail-
    closed; we don't want an unset secret to silently allow
    anonymous polling).

    Post-#5-fixup: both misconfiguration (env unset) and
    auth-failure (wrong/missing header) return 401 -- the
    previous differential status code (500 vs 401) let an
    unauthenticated prober fingerprint operator config.
    Misconfiguration still gets logged at warning so the
    operator notices it in logs.
    """
    expected = os.environ.get("HOOK_SECRET") or ""
    if not expected:
        import logging
        logging.getLogger("uvicorn.error").warning(
            "HOOK_SECRET env var unset; rejecting cron-hook call "
            "(set HOOK_SECRET on Fly to enable scheduled polling)"
        )
        raise HTTPException(401, "invalid hook secret")
    if not x_hook_secret or not hmac.compare_digest(x_hook_secret, expected):
        raise HTTPException(401, "invalid hook secret")


# ---------- response schemas ----------

class PollResult(BaseModel):
    workspace: str  # path or user_id of the tenant that was polled
    inserted: int
    error: str | None = None


class PollGmailSentResult(BaseModel):
    polled: int
    total_inserted: int
    results: list[PollResult]


class PollGmailRepliesResult(BaseModel):
    polled: int
    total_inserted: int
    results: list[PollResult]


class ReconcileItem(BaseModel):
    workspace: str
    unread_replies: int
    # FR-4b: sequences this pass auto-stopped on reply (gated by
    # cadence_settings.auto_stop_on_reply).
    sequences_stopped: int = 0
    error: str | None = None


class ReconcileDraftsResult(BaseModel):
    polled: int
    total_unread_replies: int
    # FR-4b: cross-tenant rollup of auto-stops triggered this pass.
    total_sequences_stopped: int = 0
    results: list[ReconcileItem]


# FR-5: follow-up draft generation hook output.
class FollowUpBuildItem(BaseModel):
    workspace: str
    generated: int
    skipped_done: int
    skipped_existing: int
    skipped_no_cadence: int
    errors: list[str] = []


class FollowUpBuildResult(BaseModel):
    polled: int
    total_generated: int
    results: list[FollowUpBuildItem]


class CRMPollProviderResult(BaseModel):
    """One provider's outcome within one tenant's CRM poll."""
    workspace: str
    provider: str
    inserted: int
    error: str | None = None


class CRMPollHookResult(BaseModel):
    """Aggregated across all tenants + all their connected providers."""
    polled: int  # number of (tenant, provider) pairs processed
    total_inserted: int
    results: list[CRMPollProviderResult]


# ---------- shared scatter-gather helpers ----------

def _iter_tenants_for_hook() -> list[pathlib.Path]:
    """Per-user workspaces under WORKSPACES_ROOT, OR the single
    pinned workspace in legacy mode. Hook endpoints use this so
    pre-Phase-2a deployments keep working unchanged.

    Post-#2-fixup: returns [] cleanly on cold start. Previously
    if neither WORKSPACE_PER_USER was on (or it was but the
    directory hadn't been created yet) AND INVESTOR_WORKSPACE
    was unset, `_ws_path()` raised HTTPException(500) and the
    cron hook 500'd the scheduler instead of doing a quiet
    no-op poll.
    """
    from web.routers.admin import _iter_tenant_workspaces
    tenants = _iter_tenant_workspaces()
    if tenants:
        return tenants
    # Legacy single-workspace fallback. _ws_path() raises 500
    # when the env var is unset -- catch and degrade to "no
    # tenants to poll" so a freshly-deployed app with no
    # WORKSPACES_ROOT yet doesn't ring the operator's pager.
    try:
        legacy_path = _ws_path()
    except HTTPException:
        return []
    ws_dir = pathlib.Path(legacy_path)
    return [ws_dir] if ws_dir.exists() else []


def _run_gmail_hook(poll_fn, result_cls):
    """Shared body for poll-gmail-sent / poll-gmail-replies.

    Post-#3-fixup: poll_fn itself is wrapped in try/except so a
    bug or unexpected raise from the polling helper doesn't
    tank the whole scatter-gather. One bad tenant lands as a
    PollResult with an error string; the rest still poll.
    """
    from core.config_loader import load_workspace as _load_workspace
    results: list[PollResult] = []
    total = 0
    for ws_dir in _iter_tenants_for_hook():
        try:
            ws = _load_workspace(str(ws_dir))
        except Exception as exc:  # noqa: BLE001
            results.append(PollResult(
                workspace=str(ws_dir),
                inserted=0,
                error=f"load_workspace_failed: {exc}",
            ))
            continue
        try:
            r = poll_fn(ws)
        except Exception as exc:  # noqa: BLE001
            results.append(PollResult(
                workspace=str(ws_dir),
                inserted=0,
                error=f"poll_failed: {exc}",
            ))
            continue
        results.append(PollResult(
            workspace=r.workspace,
            inserted=r.inserted,
            error=r.error,
        ))
        total += r.inserted
    return result_cls(
        polled=len(results), total_inserted=total, results=results,
    )


def _run_crm_hook(poll_fn) -> CRMPollHookResult:
    """Shared body for all five CRM hooks (B6/B7/B8). poll_fn
    returns a list of PollResult-shaped (workspace, provider,
    inserted, error) per provider for the given tenant.

    Post-#3-fixup: poll_fn is wrapped in try/except so a provider
    client that raises (rather than returning a PollResult with
    error set) doesn't 500 the entire scatter-gather.
    """
    from core.config_loader import load_workspace as _load_workspace
    flat: list[CRMPollProviderResult] = []
    total = 0
    for ws_dir in _iter_tenants_for_hook():
        try:
            ws = _load_workspace(str(ws_dir))
        except Exception as exc:  # noqa: BLE001
            flat.append(CRMPollProviderResult(
                workspace=str(ws_dir), provider="(unknown)",
                inserted=0,
                error=f"load_workspace_failed: {exc}",
            ))
            continue
        try:
            poll_results = list(poll_fn(ws))
        except Exception as exc:  # noqa: BLE001
            flat.append(CRMPollProviderResult(
                workspace=str(ws_dir), provider="(unknown)",
                inserted=0,
                error=f"poll_failed: {exc}",
            ))
            continue
        for r in poll_results:
            flat.append(CRMPollProviderResult(
                workspace=r.workspace, provider=r.provider,
                inserted=r.inserted, error=r.error,
            ))
            total += r.inserted
    return CRMPollHookResult(
        polled=len(flat), total_inserted=total, results=flat,
    )


router = APIRouter(tags=["hooks"])


# ---------- Gmail hooks (B2 + B3) ----------

@router.post(
    "/api/public/hooks/poll-gmail-sent",
    response_model=PollGmailSentResult,
    summary=(
        "Cron-triggered poll of every tenant's Gmail Sent box -> "
        "outreach_events"
    ),
)
def hook_poll_gmail_sent(
    _hook: None = Depends(_hook_secret_required),
) -> PollGmailSentResult:
    from core.outreach_events import (
        poll_gmail_sent_for_workspace as _poll,
    )
    return _run_gmail_hook(_poll, PollGmailSentResult)


@router.post(
    "/api/public/hooks/poll-gmail-replies",
    response_model=PollGmailRepliesResult,
    summary=(
        "Cron-triggered poll of every tenant's Gmail Inbox -> "
        "outreach_events (event_type='replied')"
    ),
)
def hook_poll_gmail_replies(
    _hook: None = Depends(_hook_secret_required),
) -> PollGmailRepliesResult:
    from core.outreach_events import (
        poll_gmail_replies_for_workspace as _poll,
    )
    return _run_gmail_hook(_poll, PollGmailRepliesResult)


@router.post(
    "/api/public/hooks/reconcile-drafts",
    response_model=ReconcileDraftsResult,
    summary=(
        "Cron-triggered reconciliation pass; surfaces per-tenant "
        "unread-reply counts for monitoring"
    ),
)
def hook_reconcile_drafts(
    _hook: None = Depends(_hook_secret_required),
) -> ReconcileDraftsResult:
    from core.config_loader import load_workspace as _load_workspace
    from core.outreach_events import (
        reconcile_drafts_for_workspace as _reconcile,
    )
    results: list[ReconcileItem] = []
    total = 0
    total_stopped = 0
    for ws_dir in _iter_tenants_for_hook():
        try:
            ws = _load_workspace(str(ws_dir))
        except Exception as exc:  # noqa: BLE001
            results.append(ReconcileItem(
                workspace=str(ws_dir),
                unread_replies=0,
                error=f"load_workspace_failed: {exc}",
            ))
            continue
        r = _reconcile(ws)
        results.append(ReconcileItem(
            workspace=r.workspace,
            unread_replies=r.unread_replies,
            sequences_stopped=r.sequences_stopped,
            error=r.error,
        ))
        total += r.unread_replies
        total_stopped += r.sequences_stopped
    return ReconcileDraftsResult(
        polled=len(results),
        total_unread_replies=total,
        total_sequences_stopped=total_stopped,
        results=results,
    )


@router.post(
    "/api/public/hooks/build-follow-ups",
    response_model=FollowUpBuildResult,
    summary=(
        "FR-5: daily LLM follow-up draft generation. Walks every "
        "active sequence with elapsed next_touch_due_at and writes "
        "a follow_up_drafts row (status='draft') for the Today "
        "queue's `follow_ups` array."
    ),
)
def hook_build_follow_ups(
    _hook: None = Depends(_hook_secret_required),
) -> FollowUpBuildResult:
    from core.config_loader import load_workspace as _load_workspace
    from core.followup_builder import (
        build_follow_ups_for_workspace as _build,
    )
    results: list[FollowUpBuildItem] = []
    total_generated = 0
    for ws_dir in _iter_tenants_for_hook():
        try:
            ws = _load_workspace(str(ws_dir))
        except Exception as exc:  # noqa: BLE001
            results.append(FollowUpBuildItem(
                workspace=str(ws_dir),
                generated=0, skipped_done=0,
                skipped_existing=0, skipped_no_cadence=0,
                errors=[f"load_workspace_failed: {exc}"],
            ))
            continue
        try:
            r = _build(ws)
        except Exception as exc:  # noqa: BLE001
            results.append(FollowUpBuildItem(
                workspace=str(ws_dir),
                generated=0, skipped_done=0,
                skipped_existing=0, skipped_no_cadence=0,
                errors=[f"build_failed: {exc}"],
            ))
            continue
        results.append(FollowUpBuildItem(
            workspace=r.workspace,
            generated=r.generated,
            skipped_done=r.skipped_done,
            skipped_existing=r.skipped_existing,
            skipped_no_cadence=r.skipped_no_cadence,
            errors=list(r.errors),
        ))
        total_generated += r.generated
    return FollowUpBuildResult(
        polled=len(results),
        total_generated=total_generated,
        results=results,
    )


# ---------- CRM hooks (B6 + B7 + B8) ----------

@router.post(
    "/api/public/hooks/poll-crm-activity",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered poll of every tenant's connected CRMs for "
        "new activity (notes, tasks, replies) -> outreach_events"
    ),
)
def hook_poll_crm_activity(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_activity_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)


@router.post(
    "/api/public/hooks/poll-crm-pipeline",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered poll of every tenant's connected CRMs for "
        "pipeline-stage changes -> partner_pipeline"
    ),
)
def hook_poll_crm_pipeline(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_pipeline_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)


@router.post(
    "/api/public/hooks/poll-crm-investors",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered pull of every connected CRM's investor list "
        "-> local funds + partners (6h)"
    ),
)
def hook_poll_crm_investors(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_investors_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)


@router.post(
    "/api/public/hooks/poll-crm-relationships",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered pull of CRM relationship events -> "
        "outreach_events (6h)"
    ),
)
def hook_poll_crm_relationships(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_relationships_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)


@router.post(
    "/api/public/hooks/poll-crm-lists",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered snapshot of CRM list-memberships -> "
        "crm_list_memberships (1h)"
    ),
)
def hook_poll_crm_lists(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_lists_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)


@router.post(
    "/api/public/hooks/poll-crm-deals",
    response_model=CRMPollHookResult,
    summary=(
        "Cron-triggered snapshot of CRM deal records -> "
        "crm_deals (1h)"
    ),
)
def hook_poll_crm_deals(
    _hook: None = Depends(_hook_secret_required),
) -> CRMPollHookResult:
    from core.crm_polling import (
        poll_crm_deals_for_workspace as _poll,
    )
    return _run_crm_hook(_poll)
