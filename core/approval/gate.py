"""Single source of truth for "is this draft safe to approve?".

Stage 7's `core/email/draft_routing.collect_blockers` computes blockers
at draft-generation time -- but the approval CLI and Gmail/Attio/CSV
exporters can't trust those eagerly-computed flags because state
changes between Stage 7 and approval (partner email gets set, DNC
flag gets toggled, relationship moves to active_conversation, the
draft body is regenerated). This module re-derives the gate from the
LIVE DB at approval/export time.

`can_approve_draft(ws, engine, draft_id)` loads the partner + draft +
score summary + workspace config and runs the same blocker rules
Stage 7 uses. Returns ApprovalGate(ok, blockers). Callers refuse the
operation when `ok is False` (or require an explicit per-blocker
override).

Use cases:
  - scripts/approve_draft.py: refuse human approval until blockers
    clear.
  - scripts/export_send_queue.py: re-check before writing the row so
    a stale `approval_status='approved_to_send'` (one set before a
    blocker appeared) can't slip through.
  - scripts/create_gmail_drafts.py: same gate before pushing the
    draft to Gmail.
  - scripts/08_sync_to_attio.py: same gate before shipping the
    approved body to the CRM.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from core.db import email_drafts, partner_score_summaries, partners
from core.email.draft_routing import collect_blockers


@dataclass(frozen=True)
class ApprovalGate:
    ok: bool
    blockers: tuple[str, ...]


# Blockers carried over from Stage 7's per-draft QA that the approval
# gate also enforces. Stored on email_drafts (qa_status / template_smell)
# at Stage 7 time so re-deriving them at approve time is a single
# column read.
QA_FAIL_BLOCKERS_BY_STATUS: dict[str, str] = {
    "fail": "Stage 7 batch QA marked this draft qa_status=fail",
}


def _qa_blockers(qa_status: str | None, template_smell: str | None) -> list[str]:
    out: list[str] = []
    msg = QA_FAIL_BLOCKERS_BY_STATUS.get((qa_status or "").lower())
    if msg:
        out.append(msg)
    # template_smell=high is also surfaced by collect_blockers via the
    # rec_template_smell input below, but the canonical store is the
    # email_drafts column -- so feed it through.
    return out


def can_approve_draft(
    ws, engine, draft_id: int, *, allow_example_domains: bool = False,
) -> ApprovalGate:
    """Re-derive the approval gate from live DB state.

    `allow_example_domains` defaults to False -- approval blocks
    `.example` fixture data unless the operator explicitly opted in
    (typically via `--allow-example-domains` on the CLI, for
    fixture smoke tests).

    Returns ApprovalGate(ok=True, blockers=()) when the draft can be
    approved; otherwise ok=False with the operator-actionable list.
    """
    with engine.begin() as conn:
        draft = conn.execute(
            select(
                email_drafts.c.draft_id,
                email_drafts.c.partner_id,
                email_drafts.c.subject,
                email_drafts.c.body,
                email_drafts.c.template_smell,
                email_drafts.c.qa_status,
            ).where(email_drafts.c.draft_id == draft_id)
        ).first()
        if draft is None:
            return ApprovalGate(
                ok=False,
                blockers=(f"draft_id={draft_id} not found",),
            )
        partner = conn.execute(
            select(
                partners.c.partner_id,
                partners.c.email,
                partners.c.do_not_contact,
                partners.c.relationship_status,
                partners.c.last_contacted_at,
                partners.c.last_reply_at,
                partners.c.email_verification_status,
            ).where(partners.c.partner_id == draft.partner_id)
        ).first()
        if partner is None:
            return ApprovalGate(
                ok=False,
                blockers=(
                    f"partner {draft.partner_id!r} referenced by draft "
                    f"no longer exists",
                ),
            )
        summary = conn.execute(
            select(
                partner_score_summaries.c.recommended_to_send,
                partner_score_summaries.c.cold_reachability_score,
                partner_score_summaries.c.recommendation_reasoning,
            ).where(partner_score_summaries.c.partner_id == draft.partner_id)
        ).first()

    qa_blockers = _qa_blockers(draft.qa_status, draft.template_smell)

    # Re-run the same gate Stage 7 used, but with LIVE partner + draft
    # state. We skip the cross-batch similarity check because the
    # canonical "this draft is in a similarity-failure pair" signal
    # already lives in draft.qa_status -- if Stage 7 failed the batch
    # because of sim, qa_status='fail' lands above.
    banned = (
        (ws.company.get("founder_voice") or {}).get("banned_phrases", []) or []
    )
    routing_blockers = collect_blockers(
        rec_subject=draft.subject,
        rec_body=draft.body,
        rec_template_smell=draft.template_smell,
        in_sim_failure_pair=False,
        company_cfg=ws.company,
        allow_example_domains=allow_example_domains,
        pctx_recommended_to_send=bool(summary and summary.recommended_to_send),
        pctx_cold_reachability_score=(
            summary.cold_reachability_score if summary else None
        ),
        pctx_partner_email=partner.email,
        pctx_do_not_contact=bool(partner.do_not_contact),
        pctx_relationship_status=partner.relationship_status,
        pctx_last_contacted_at=partner.last_contacted_at,
        pctx_last_reply_at=partner.last_reply_at,
        pctx_email_verification_status=partner.email_verification_status,
        banned=banned,
    )

    all_blockers = tuple(qa_blockers) + tuple(routing_blockers)
    return ApprovalGate(ok=not all_blockers, blockers=all_blockers)
