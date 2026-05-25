"""Stage 7 draft-routing decision (Slice 1 rewrite for cold-outreach
approval workflow).

Stage 7 never auto-approves. Every draft starts in `needs_review`.
This module computes the operator-visible review-queue picture for a
draft: what blockers (if any) prevent approval, and the human-
readable reasoning the operator reads in the CSV / UI.

Returns a DraftRoutingDecision with:

  - approval_status_hint : "needs_review" | "qa_failed"
        Stage 7 always seeds the DB row as `needs_review` (the
        approval state machine has only one valid initial state).
        qa_failed is used in the CSV / UI to surface "this draft
        has hard-gate failures and SHOULD NOT be approved as-is".
        The DB row stays in needs_review; the operator sees the
        block in their review tools and either fixes it before
        approving or rejects.

  - blockers : tuple[str, ...]
        Operator-actionable reasons the draft can't yet be approved.
        Examples: 'missing partner email', 'template_smell=high',
        'body contains hallucinated scheduling URL', 'do_not_contact
        is set'.  Empty tuple means no known blockers.

  - reasoning : str
        Free-text rendered into the review CSV / UI explaining
        Stage 7's recommendation context + any blockers.

Slice 1: warm-path output is GONE. The pipeline no longer drafts
intro-request emails or treats warm_path_available as a
distinguishing branch. Every partner gets a cold draft seeded as
needs_review; a human decides.

The function composes existing helpers:
  - core.email.batch_qa.check_hard_gates() : per-draft body gates
  - core.production_guards.production_gate_for_ready_to_send() :
    fixture-data / production-readiness checks
"""
from __future__ import annotations

from dataclasses import dataclass

from core.approval.state_machine import (
    STATE_NEEDS_REVIEW,
)
from core.email.batch_qa import check_hard_gates
from core.production_guards import production_gate_for_ready_to_send


# Scheduling-link hosts the body might contain. When one of these
# appears in a draft's body but doesn't match the workspace's
# configured preferred_scheduling_link, that's a hallucinated link --
# the LLM invented a calendar URL that points at the wrong calendar.
SCHEDULING_LINK_HOSTS: tuple[str, ...] = (
    "cal.com/", "calendly.com/", "savvycal.com/",
    "meetings.hubspot.com/", "cal.example/",
)

# Approval status hints surfaced in the CSV / UI. The DB pointer
# always lands as STATE_NEEDS_REVIEW (state-machine invariant); these
# strings annotate the operator's view of WHY a draft is in review.
HINT_NEEDS_REVIEW: str = STATE_NEEDS_REVIEW  # = "needs_review"
HINT_QA_FAILED: str = "qa_failed"
HINT_MISSING_EMAIL: str = "missing_email"
HINT_DO_NOT_CONTACT: str = "do_not_contact"

# Back-compat: legacy Stage 7 + tests imported these names. Map them
# to the new vocabulary so external readers don't crash during the
# transition. STATUS_WARM_PATH points to needs_review because warm-
# path is gone -- the partner just needs review like every other.
STATUS_DRAFT = HINT_NEEDS_REVIEW
STATUS_READY = HINT_NEEDS_REVIEW
STATUS_WARM_PATH = HINT_NEEDS_REVIEW


@dataclass(frozen=True)
class DraftRoutingDecision:
    """Operator-visible review picture for one draft.

    `approval_status_hint` is purely a CSV/UI label. The DB row's
    approval_status is always seeded as needs_review (see
    core/approval/state_machine.py); a human is the only path to
    approved_to_send.
    """
    approval_status_hint: str
    blockers: tuple[str, ...]
    reasoning: str

    @property
    def qa_fails(self) -> tuple[str, ...]:
        """Back-compat alias for code reading the prior shape."""
        return self.blockers

    @property
    def outreach_status(self) -> str:
        """Back-compat alias used by the older CSV-write path.
        Resolves to the approval hint so downstream readers don't
        crash during the transition window."""
        return self.approval_status_hint

    @property
    def downgraded(self) -> bool:
        """Stage 6 recommended the partner but Stage 7 found
        blockers -- operator-visible counter."""
        return self.approval_status_hint == HINT_QA_FAILED


def _company_primary_domain(company_cfg: dict) -> str | None:
    """Best-effort: extract the company's primary domain from the
    scheduling link (post-redirect host) or fall back to the founder
    email's domain. Returns lowercase host, no port.

    When the scheduling link points at a third-party scheduling service
    (cal.com, calendly.com, etc.) OR an RFC 2606 reserved TLD
    (.example), the link doesn't carry the company's primary domain --
    fall through to the founder email's domain instead.
    """
    co = (company_cfg or {}).get("company") or {}
    link = (co.get("meeting_ask") or {}).get(
        "preferred_scheduling_link"
    ) or ""
    scheduling_hosts = (
        "cal.com", "calendly.com", "savvycal.com", "hubspot.com",
        "google.com", "x.ai", "tldv.io",
    )
    reserved_tlds = (".example", ".test", ".invalid", ".localhost")
    if "://" in link:
        rest = link.split("://", 1)[1]
        for sep in ("/", "?", "#", ":"):
            if sep in rest:
                rest = rest.split(sep, 1)[0]
        rest = rest.strip().lower()
        is_scheduling_service = rest in scheduling_hosts or any(
            rest.endswith(suffix) for suffix in reserved_tlds
        )
        if rest and not is_scheduling_service:
            return rest
        # Scheduling-service host: fall through to founder email below.
    fe = (co.get("founder_email") or "").strip().lower()
    if "@" in fe:
        return fe.split("@", 1)[1] or None
    return None


def _collect_blockers(
    *,
    rec_subject: str | None,
    rec_body: str | None,
    rec_template_smell: str | None,
    in_sim_failure_pair: bool,
    company_cfg: dict,
    allow_example_domains: bool,
    pctx_recommended_to_send: bool,
    pctx_cold_reachability_score: float | None,
    pctx_partner_email: str | None,
    pctx_do_not_contact: bool,
    banned: list[str],
) -> list[str]:
    blockers: list[str] = []
    # Cold-outreach absolutes: a partner flagged do_not_contact MUST
    # never make it past the review queue regardless of draft quality.
    if pctx_do_not_contact:
        blockers.append(
            "partner.do_not_contact is set -- approval blocked",
        )
    # Missing email is a hard blocker for approval. Stage 7 still
    # generates the draft (so the operator sees who needs Apollo
    # enrichment) but the draft can't be approved without an email.
    if not (pctx_partner_email or "").strip():
        blockers.append(
            "partner email is unknown -- Apollo upload required "
            "before approval"
        )
    blockers.extend(check_hard_gates(
        {"subject": rec_subject, "body": rec_body}, banned,
    ))
    if rec_template_smell == "high":
        blockers.append("template_smell=high")
    if in_sim_failure_pair:
        blockers.append("body similarity > 0.82 with another draft")
    # Batch 9: production guards.
    prod_fails = production_gate_for_ready_to_send(
        subject=rec_subject,
        body=rec_body,
        scheduling_link=(
            (company_cfg.get("company") or {})
            .get("meeting_ask", {})
            .get("preferred_scheduling_link")
        ),
        founder_email=(company_cfg.get("company") or {}).get("founder_email"),
        partner_email=None,
        allow_example_domains=allow_example_domains,
    )
    blockers.extend(prod_fails)
    # Batch 37 (#44): scheduling-link hallucination check.
    configured_link = (
        (company_cfg.get("company") or {})
        .get("meeting_ask", {})
        .get("preferred_scheduling_link")
        or ""
    ).strip().lower()
    body_lower = (rec_body or "").lower()
    for host in SCHEDULING_LINK_HOSTS:
        if host in body_lower and (
            not configured_link or host not in configured_link
        ):
            blockers.append(
                f"body contains scheduling link host {host!r} but "
                f"workspace configured "
                f"{(configured_link or '<none>')!r} -- LLM may have "
                f"hallucinated a scheduling URL"
            )
            break
    # Batch 37 (#42): defense in depth -- a Stage-6-recommended
    # partner with no reachability score should not be approvable.
    if (
        pctx_recommended_to_send
        and pctx_cold_reachability_score is None
    ):
        blockers.append(
            "cold_reachability_score is unknown; approval refused "
            "until Stage 4 produces a reachability partial"
        )
    # Batch 37 (#35): founder-email-domain alignment soft check.
    founder_email = (
        company_cfg.get("company") or {}
    ).get("founder_email") or ""
    primary_domain = _company_primary_domain(company_cfg)
    if founder_email and primary_domain and "@" in founder_email:
        fe_domain = founder_email.split("@", 1)[1].lower().strip()
        if fe_domain != primary_domain:
            blockers.append(
                f"founder email domain {fe_domain!r} does not "
                f"match company primary domain {primary_domain!r}; "
                f"verify the sender is intentional"
            )
    return blockers


def decide_draft_routing(
    *,
    rec_subject: str | None,
    rec_body: str | None,
    rec_template_smell: str | None,
    in_sim_failure_pair: bool,
    pctx_recommendation_reasoning: str | None,
    pctx_recommended_to_send: bool,
    pctx_warm_path_available: bool | None = None,  # ignored; back-compat
    pctx_cold_reachability_score: float | None,
    pctx_partner_email: str | None = None,
    pctx_do_not_contact: bool = False,
    banned: list[str],
    company_cfg: dict,
    allow_example_domains: bool,
) -> DraftRoutingDecision:
    """Compute the review-queue picture for one partner's draft.

    Slice 1 cold-outreach model:

      - Stage 7 NEVER auto-approves. Every draft is seeded as
        needs_review in the DB; the CSV / UI label reflects whether
        the operator should expect to approve it cleanly
        (needs_review) or fix something first (qa_failed).
      - warm_path branch removed. `pctx_warm_path_available` is
        accepted but ignored for back-compat with older callers.
      - Missing partner email + do_not_contact are explicit blockers
        rather than a separate route. The draft still lands in the
        review queue with the blocker visible so the operator knows
        what to fix (Apollo upload / clear DNC).

    Returns DraftRoutingDecision(approval_status_hint, blockers,
    reasoning). The DB row's actual approval_status pointer always
    seeds as STATE_NEEDS_REVIEW -- this function only computes the
    operator-visible LABEL.
    """
    blockers = _collect_blockers(
        rec_subject=rec_subject,
        rec_body=rec_body,
        rec_template_smell=rec_template_smell,
        in_sim_failure_pair=in_sim_failure_pair,
        company_cfg=company_cfg,
        allow_example_domains=allow_example_domains,
        pctx_recommended_to_send=pctx_recommended_to_send,
        pctx_cold_reachability_score=pctx_cold_reachability_score,
        pctx_partner_email=pctx_partner_email,
        pctx_do_not_contact=pctx_do_not_contact,
        banned=banned,
    )
    base_reason = pctx_recommendation_reasoning or ""

    if blockers:
        # The operator sees the qa_failed label + the per-blocker list
        # so they can fix the issues before approving. The DB row is
        # still seeded as needs_review by Stage 7's seed_draft call.
        return DraftRoutingDecision(
            approval_status_hint=HINT_QA_FAILED,
            blockers=tuple(blockers),
            reasoning=(
                f"BLOCKERS preventing approval: {'; '.join(blockers)}. "
                f"(Stage 6 said: {base_reason or '-'})"
            ),
        )

    return DraftRoutingDecision(
        approval_status_hint=HINT_NEEDS_REVIEW,
        blockers=(),
        reasoning=(
            base_reason
            if pctx_recommended_to_send
            else (
                f"Stage 6 did not recommend this partner: "
                f"{base_reason or '(no reason)'}"
            )
        ),
    )
