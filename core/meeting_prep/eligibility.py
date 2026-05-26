"""Post-reply eligibility + review-task helpers (Build Session 14).

The dossier is a POST-REPLY artifact. We do not generate it during
cold outreach, on every recommended partner, or as part of Stage 7.
This module is the single source of truth for "is this partner
ready for a dossier?" so producers (the outcome-persistence layer)
and consumers (prep_brief.py --dossier, status.py) agree.

Two surfaces:

  - is_dossier_eligible(...): pure predicate. Returns True when the
    partner's latest outcome or relationship row indicates
    substantive engagement (reply / meeting / active conversation),
    or when an operator has set their `relationship_status`
    explicitly to one of the active states.

  - ensure_review_task(...) / mark_task_resolved(...): the
    write-side. Producers (persist_outcome_event) call ensure to
    create a `kind='investor_dossier_needed'` row when one is
    needed and absent. Consumers (prep_brief.py --dossier) call
    mark_resolved after a successful artifact build.

The eligibility predicate is deliberately permissive on the "should
generate" side and conservative on the "create a task" side: an
operator running prep_brief --dossier with --force-refresh can
bypass eligibility, but auto-task creation from outcome ingestion
only fires for the unambiguous reply/meeting cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine

from core.attribution.review_queue import (
    list_pending,
    queue_review,
    resolve as resolve_review,
)
from core.db import outcomes, partners, review_items

# Outreach statuses that indicate the partner has earned a dossier.
# Excludes "sent" (still cold), "dead", "warm_path_needed".
ELIGIBLE_OUTREACH_STATUSES: frozenset[str] = frozenset({
    "replied",
    "interested",        # not currently a STATUS_VALUES enum value but
                         # accepted here for future-proofing and Attio
                         # rows whose `outreach_status` may map to it.
    "meeting_booked",
})

# Relationship states (managed via core/relationships.py +
# state_from_outcome_event) that count as substantive engagement
# regardless of the outreach_status column. The relationship state
# survives downstream changes that may overwrite outreach_status.
ELIGIBLE_RELATIONSHIP_STATES: frozenset[str] = frozenset({
    "active_conversation",
    "meeting_booked",
})

# reply_type values that count as substantive interest. "booked" and
# "asked_for_more_info" + "asked_for_deck" all signal genuine
# engagement; "passed_*" and "no_response" do not.
SUBSTANTIVE_REPLY_TYPES: frozenset[str] = frozenset({
    "booked",
    "asked_for_deck",
    "asked_for_more_info",
    "referred_to_colleague",
    "warm_intro_requested",
})

# Negative signals: explicit reasons to NEVER auto-generate a
# dossier task, even if some other field hints at engagement.
DISQUALIFYING_REPLY_TYPES: frozenset[str] = frozenset({
    "no_response",
    "passed_too_early",
    "passed_category",
    "wrong_stage",
})

DOSSIER_TASK_KIND = "investor_dossier_needed"


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str       # short, human-readable -- surfaces in task.reason
    triggering_outreach_status: str | None = None
    triggering_reply_type: str | None = None
    triggering_relationship: str | None = None


def is_dossier_eligible(engine: Engine, partner_id: str) -> EligibilityResult:
    """Compute whether the partner currently warrants a dossier.

    Inspection order (any match = eligible):
      1. partner row's `do_not_contact` set -> NEVER eligible
      2. partner row's `relationship_status` in eligible set
      3. latest outcome's `outreach_status` in eligible set
      4. latest outcome's `reply_type` in substantive set
      5. latest outcome's `meeting_booked` True

    `do_not_contact` short-circuits at step 1 because it represents
    an explicit operator decision that overrides positive signals
    elsewhere (e.g. an old 'meeting_booked' row that's been since
    superseded by a manual flag).
    """
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(
                partners.c.do_not_contact,
                partners.c.relationship_status,
            ).where(partners.c.partner_id == partner_id)
        ).first()
        latest_outcome = conn.execute(
            select(
                outcomes.c.outreach_status,
                outcomes.c.reply_type,
                outcomes.c.meeting_booked,
            ).where(outcomes.c.partner_id == partner_id)
            .order_by(desc(outcomes.c.outcome_id)).limit(1)
        ).first()

    if partner_row is None:
        return EligibilityResult(
            eligible=False, reason="partner not found",
        )
    if bool(partner_row.do_not_contact):
        return EligibilityResult(
            eligible=False, reason="partner is do_not_contact",
        )
    rel = partner_row.relationship_status or ""
    if rel in ELIGIBLE_RELATIONSHIP_STATES:
        return EligibilityResult(
            eligible=True,
            reason=f"relationship_status={rel}",
            triggering_relationship=rel,
        )
    if latest_outcome is None:
        return EligibilityResult(
            eligible=False, reason="no outcome history",
        )
    status = latest_outcome.outreach_status or ""
    reply = latest_outcome.reply_type or ""
    if reply in DISQUALIFYING_REPLY_TYPES:
        return EligibilityResult(
            eligible=False,
            reason=f"latest reply_type={reply}",
            triggering_reply_type=reply,
        )
    if status in ELIGIBLE_OUTREACH_STATUSES:
        return EligibilityResult(
            eligible=True,
            reason=f"outreach_status={status}",
            triggering_outreach_status=status,
        )
    if reply in SUBSTANTIVE_REPLY_TYPES:
        return EligibilityResult(
            eligible=True,
            reason=f"reply_type={reply}",
            triggering_reply_type=reply,
        )
    if bool(latest_outcome.meeting_booked):
        return EligibilityResult(
            eligible=True,
            reason="meeting_booked=true",
            triggering_outreach_status=status or None,
        )
    return EligibilityResult(
        eligible=False,
        reason=(
            f"latest outcome status={status!r}, reply_type={reply!r} "
            f"-- not substantive"
        ),
    )


def ensure_review_task(
    engine: Engine, *, partner_id: str, source: str,
    reason: str, kind: str = DOSSIER_TASK_KIND,
) -> int | None:
    """Idempotently create a review_items row via the shared
    review-queue helper.

    `partner_id` is stored as the queue's `target_id` (free-text
    foreign id); `source` + `reason` ride in the `context` JSON blob
    so consumers (status.py, prep_brief --pending-only) can audit
    why the task exists. Returns the review_id of the new (or
    existing-pending) row.

    The shared queue is idempotent on (kind, target_id, status=
    'pending'): a repeat call returns the same id rather than
    creating a duplicate. We surface that as None from this wrapper
    so callers can tell "created" from "already existed" if they
    care -- the shared helper returns the id either way.
    """
    with engine.begin() as conn:
        existing = conn.execute(
            select(review_items.c.review_id).where(
                review_items.c.kind == kind,
                review_items.c.target_id == partner_id,
                review_items.c.status == "pending",
            ).limit(1)
        ).first()
    if existing is not None:
        return None
    return queue_review(
        engine, kind=kind, target_id=partner_id,
        context={"source": source, "reason": reason},
    )


def pending_dossier_task_ids(
    engine: Engine, *, kind: str = DOSSIER_TASK_KIND,
) -> list[tuple[int, str]]:
    """Open dossier tasks as (review_id, partner_id) tuples. Drives
    prep_brief.py --pending-only's batch loop."""
    rows = list_pending(engine, kind=kind)
    return [(int(r.review_id), r.target_id) for r in rows]


def mark_task_resolved(
    engine: Engine, *, review_item_id: int,
    resolved_artifact_id: int | None = None,
    resolved_by: str = "prep_brief",
) -> None:
    """Resolve a pending review row. `resolved_artifact_id` rides in
    the resolution_notes so status.py can prove the dossier was
    actually built (the legacy `review_items` schema doesn't have a
    dedicated FK column, and adding one would be a bigger migration
    than this session needs)."""
    notes = (
        f"artifact_id={resolved_artifact_id}"
        if resolved_artifact_id is not None
        else "dossier built"
    )
    resolve_review(
        engine, review_id=review_item_id,
        resolved_by=resolved_by, notes=notes,
    )


def count_open_tasks(
    engine: Engine, *, kind: str = DOSSIER_TASK_KIND,
) -> int:
    """How many pending tasks of this kind. For status.py's
    headline."""
    return len(list_pending(engine, kind=kind))
