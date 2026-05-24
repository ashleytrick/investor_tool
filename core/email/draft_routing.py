"""Stage 7 draft-routing decision (Refactor item 14).

Pure function that takes a partner's recommended draft + the Stage 6
context + workspace config and returns:

  - outreach_status :  "warm_path_needed" | "draft" | "ready_to_send"
  - qa_fails       :  list of human-readable failure reasons (empty
                      when the draft passes everything)
  - reasoning      :  the final recommendation_reasoning string to
                      write into the CSV
  - downgraded     :  True iff a Stage-6-recommended partner got
                      bumped to outreach_status="draft" by Stage 7's
                      QA layer (operator-visible counter)

This used to be ~120 lines of inline checks inside Stage 7's CSV-row
build loop. Moving it out makes the routing decision testable from a
small synthetic context dict, and the per-criterion failure messages
become unit-coverable.

The function deliberately doesn't import Stage 7. It composes
existing helpers:
  - core.email.batch_qa.check_hard_gates() : per-draft body gates
  - core.production_guards.production_gate_for_ready_to_send() :
    fixture-data / production-readiness checks
"""
from __future__ import annotations

from dataclasses import dataclass

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

# Outreach status values used downstream (Stage 8 reads these).
STATUS_WARM_PATH = "warm_path_needed"
STATUS_DRAFT = "draft"
STATUS_READY = "ready_to_send"


@dataclass(frozen=True)
class DraftRoutingDecision:
    outreach_status: str
    qa_fails: tuple[str, ...]
    reasoning: str
    downgraded: bool


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


def _collect_qa_fails(
    *,
    rec_subject: str | None,
    rec_body: str | None,
    rec_template_smell: str | None,
    in_sim_failure_pair: bool,
    company_cfg: dict,
    allow_example_domains: bool,
    pctx_recommended_to_send: bool,
    pctx_cold_reachability_score: float | None,
    banned: list[str],
) -> list[str]:
    qa_fails: list[str] = list(check_hard_gates(
        {"subject": rec_subject, "body": rec_body}, banned,
    ))
    if rec_template_smell == "high":
        qa_fails.append("template_smell=high")
    if in_sim_failure_pair:
        qa_fails.append("body similarity > 0.82 with another draft")
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
    qa_fails.extend(prod_fails)
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
            qa_fails.append(
                f"body contains scheduling link host {host!r} but "
                f"workspace configured "
                f"{(configured_link or '<none>')!r} -- LLM may have "
                f"hallucinated a scheduling URL"
            )
            break
    # Batch 37 (#42): defense in depth -- a recommended partner with
    # no reachability score should never land as ready_to_send.
    if (
        pctx_recommended_to_send
        and pctx_cold_reachability_score is None
    ):
        qa_fails.append(
            "cold_reachability_score is unknown; Stage 7 refuses "
            "to mark ready_to_send without it"
        )
    # Batch 37 (#35): founder-email-domain alignment soft check.
    founder_email = (
        company_cfg.get("company") or {}
    ).get("founder_email") or ""
    primary_domain = _company_primary_domain(company_cfg)
    if founder_email and primary_domain and "@" in founder_email:
        fe_domain = founder_email.split("@", 1)[1].lower().strip()
        if fe_domain != primary_domain:
            qa_fails.append(
                f"founder email domain {fe_domain!r} does not "
                f"match company primary domain {primary_domain!r}; "
                f"verify the sender is intentional"
            )
    return qa_fails


def decide_draft_routing(
    *,
    rec_subject: str | None,
    rec_body: str | None,
    rec_template_smell: str | None,
    in_sim_failure_pair: bool,
    pctx_recommendation_reasoning: str | None,
    pctx_recommended_to_send: bool,
    pctx_warm_path_available: bool | None,
    pctx_cold_reachability_score: float | None,
    banned: list[str],
    company_cfg: dict,
    allow_example_domains: bool,
) -> DraftRoutingDecision:
    """Compute outreach_status + reasoning for one partner's draft.

    The decision order is:
      1. warm_path_available=True  -> warm_path_needed (cold draft suppressed)
      2. recommended_to_send + qa_fails -> draft (downgraded; reasoning lists fails)
      3. recommended_to_send + no fails -> ready_to_send
      4. otherwise -> draft (Stage 6 already said not-recommended)
    """
    qa_fails = _collect_qa_fails(
        rec_subject=rec_subject,
        rec_body=rec_body,
        rec_template_smell=rec_template_smell,
        in_sim_failure_pair=in_sim_failure_pair,
        company_cfg=company_cfg,
        allow_example_domains=allow_example_domains,
        pctx_recommended_to_send=pctx_recommended_to_send,
        pctx_cold_reachability_score=pctx_cold_reachability_score,
        banned=banned,
    )
    base_reason = pctx_recommendation_reasoning or ""

    if pctx_warm_path_available:
        return DraftRoutingDecision(
            outreach_status=STATUS_WARM_PATH,
            qa_fails=tuple(qa_fails),
            reasoning=(
                f"warm_path_available=TRUE; cold draft suppressed. "
                f"{base_reason}"
            ).strip(),
            downgraded=False,
        )
    if pctx_recommended_to_send and qa_fails:
        return DraftRoutingDecision(
            outreach_status=STATUS_DRAFT,
            qa_fails=tuple(qa_fails),
            reasoning=(
                f"DOWNGRADED by Stage 7 QA: {'; '.join(qa_fails)}. "
                f"(Stage 6 said: {base_reason or '-'})"
            ),
            downgraded=True,
        )
    if pctx_recommended_to_send:
        return DraftRoutingDecision(
            outreach_status=STATUS_READY,
            qa_fails=(),
            reasoning=base_reason,
            downgraded=False,
        )
    return DraftRoutingDecision(
        outreach_status=STATUS_DRAFT,
        qa_fails=tuple(qa_fails),
        reasoning=base_reason,
        downgraded=False,
    )
