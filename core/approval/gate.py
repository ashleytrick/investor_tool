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
operation when `ok is False`.

Override durability (PR #7 follow-up review finding)
----------------------------------------------------

Blockers fall into two classes:

  * HARD -- cannot be overridden: missing partner email, do-not-contact,
    QA-fail, missing partner record, empty body/subject, missing
    cold-reachability partial, invalid email verification. These represent
    "we literally cannot send" or "we promised not to send."

  * SOFT -- the operator can override with explicit acknowledgement
    (--override-blockers --notes ...). Examples: generic/role mailbox,
    risky verification, .example fixture domains, scheduling-link host
    mismatch, founder/company-domain mismatch, template_smell=high,
    cross-draft body similarity.

When an operator overrides, the soft blockers they acknowledged are
persisted on the approval event (`draft_approvals.overridden_blockers`).
Downstream re-checks call `can_approve_draft(..., respect_overrides=True)`
which removes those exact strings from the gate's output -- so an
overridden draft survives the round-trip to Gmail/Attio/CSV without
being immediately re-flagged as stale.

Use cases:
  - scripts/approve_draft.py: classify; refuse hard blockers; persist
    overridden soft blockers.
  - scripts/export_send_queue.py: respect_overrides=True.
  - scripts/create_gmail_drafts.py: respect_overrides=True.
  - scripts/08_sync_to_attio.py: respect_overrides=True.
  - scripts/check_ready.py: respect_overrides=True (surface "approved
    with override" rather than BLOCKED).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import desc, select

from core.db import draft_approvals, email_drafts, partner_score_summaries, partners
from core.email.draft_routing import collect_blockers


@dataclass(frozen=True)
class ApprovalGate:
    ok: bool
    blockers: tuple[str, ...]
    # Blockers that were SUPPRESSED via a prior operator override. ok=True
    # when only this list is non-empty -- the operator already
    # acknowledged these.
    overridden: tuple[str, ...] = field(default_factory=tuple)


# Blockers carried over from Stage 7's per-draft QA that the approval
# gate also enforces. Stored on email_drafts (qa_status / template_smell)
# at Stage 7 time so re-deriving them at approve time is a single
# column read.
QA_FAIL_BLOCKERS_BY_STATUS: dict[str, str] = {
    "fail": "Stage 7 batch QA marked this draft qa_status=fail",
}


# Substrings that mark a blocker as HARD -- the approval CLI refuses
# `--override-blockers` outright when any of these match. The list is
# narrow on purpose: only the genuinely-cannot-send and
# we-promised-not-to-send classes.
HARD_BLOCKER_SUBSTRINGS: tuple[str, ...] = (
    "partner email is unknown",
    "do_not_contact",
    "no longer exists",
    "Stage 7 batch QA marked this draft qa_status=fail",
    "cold_reachability_score is unknown",
    "verification status = invalid",
    "draft_id=",  # "draft_id=N not found"
    "superseded",  # Slice 17 immutable history: stale generation
)


def classify_blocker(blocker: str) -> str:
    """'hard' if --override-blockers cannot bypass; 'soft' otherwise.

    Hard blockers represent "literally cannot send" or "we promised
    not to send" -- missing email, do-not-contact, missing partner,
    QA fail, invalid verification. Soft blockers (generic mailbox,
    .example domains, risky verification, etc.) can be overridden
    with explicit operator acknowledgement.
    """
    low = blocker.lower()
    for needle in HARD_BLOCKER_SUBSTRINGS:
        if needle.lower() in low:
            return "hard"
    return "soft"


def split_blockers(blockers) -> tuple[list[str], list[str]]:
    """Partition into (hard, soft). Order within each list is preserved."""
    hard: list[str] = []
    soft: list[str] = []
    for b in blockers:
        if classify_blocker(b) == "hard":
            hard.append(b)
        else:
            soft.append(b)
    return hard, soft


def _qa_blockers(qa_status: str | None, template_smell: str | None) -> list[str]:
    out: list[str] = []
    msg = QA_FAIL_BLOCKERS_BY_STATUS.get((qa_status or "").lower())
    if msg:
        out.append(msg)
    # template_smell=high is also surfaced by collect_blockers via the
    # rec_template_smell input below, but the canonical store is the
    # email_drafts column -- so feed it through.
    return out


def latest_overridden_blockers(engine, draft_id: int) -> list[str]:
    """Read the soft blockers the operator acknowledged on the most
    recent approved_to_send event for this draft. Returns [] when
    there's no approved event yet, or the event has no override.

    Subsequent approve events overwrite earlier ones (one row per
    approval action); stale_after_approval / rejected events don't
    erase the override metadata, but `respect_overrides=True` only
    looks at the most recent approved_to_send row -- so a fresh
    re-approval without override correctly drops the override."""
    with engine.begin() as conn:
        row = conn.execute(
            select(draft_approvals.c.overridden_blockers)
            .where(
                draft_approvals.c.draft_id == draft_id,
                draft_approvals.c.event_type == "approved_to_send",
            )
            .order_by(desc(draft_approvals.c.event_id))
            .limit(1)
        ).first()
    if row is None or not row.overridden_blockers:
        return []
    try:
        data = json.loads(row.overridden_blockers)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(b) for b in data]


def can_approve_draft(
    ws, engine, draft_id: int,
    *,
    allow_example_domains: bool = False,
    respect_overrides: bool = False,
) -> ApprovalGate:
    """Re-derive the approval gate from live DB state.

    `allow_example_domains` -- approval blocks `.example` fixture data
    unless the operator opts in (typically via `--allow-example-domains`
    on the CLI).

    `respect_overrides` -- when True, soft blockers the operator
    acknowledged on the most recent approved_to_send event are
    REMOVED from the returned `blockers` list and surfaced separately
    in `overridden`. Downstream send/export consumers pass True so an
    approved-with-override draft doesn't immediately flip to stale.
    Hard blockers (missing email, DNC, etc.) are never suppressed.

    Returns ApprovalGate(ok=True, blockers=(), overridden=(...)) when
    the draft passes; otherwise ok=False with the still-active list.
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
                email_drafts.c.superseded_at,
            ).where(email_drafts.c.draft_id == draft_id)
        ).first()
        if draft is None:
            return ApprovalGate(
                ok=False,
                blockers=(f"draft_id={draft_id} not found",),
            )
        # Slice 17 immutable history hard refusal: a superseded draft
        # is a prior generation kept for audit. It must NEVER be
        # approvable / sendable / syncable. Refuse before any other
        # check so the operator sees the exact reason + so the
        # downstream gate (export_send_queue, Gmail, Attio) can never
        # accidentally treat a stale body as live. Classified HARD in
        # HARD_BLOCKER_SUBSTRINGS so --override-blockers can't bypass.
        if draft.superseded_at is not None:
            return ApprovalGate(
                ok=False,
                blockers=(
                    f"draft_id={draft_id} is superseded "
                    f"(superseded_at={draft.superseded_at.isoformat()}); "
                    f"approve the latest generation for this partner instead",
                ),
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

    all_blockers = list(qa_blockers) + list(routing_blockers)
    overridden: list[str] = []
    if respect_overrides and all_blockers:
        acknowledged = set(latest_overridden_blockers(engine, draft_id))
        kept: list[str] = []
        for b in all_blockers:
            # Hard blockers can never be overridden -- belt-and-suspenders
            # in case approve_draft.py's pre-check is bypassed by a
            # future caller that writes overridden_blockers directly.
            if b in acknowledged and classify_blocker(b) == "soft":
                overridden.append(b)
            else:
                kept.append(b)
        all_blockers = kept

    return ApprovalGate(
        ok=not all_blockers,
        blockers=tuple(all_blockers),
        overridden=tuple(overridden),
    )
