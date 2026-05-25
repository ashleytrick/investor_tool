"""Stage 7: generate emails + write the CSV review queue.

For the top N partners by send_now_priority (default 25; Gate 5 fixture runs
top 5), per partner:
  1. Score the 6 strategies for eligibility (0-3); a strategy is usable only
     at >= 2, and signal_led specifically requires a quality->=3 signal.
  2. Pick two distinct strategies (or one with limited_variation=true).
  3. Produce two variants, a deck_request_response, a follow-up draft, a
     conversion hypothesis, and a likely-objection + preemption tag.
  4. Validate against schemas/email_generation.py.

After per-partner generation, the batch is QA'd:
  - pairwise body / first-sentence / subject similarity (rapidfuzz token_set)
  - template-smell judge (LLM live, heuristic in stub mode) on each draft
    against its 5 nearest neighbors
  - hard gates (similarity, smell, raise reference, soft CTA, eligibility)
  - warning gates (strategy concentration, CTA repetition, smell distribution)

Outputs:
  - email_drafts / followup_drafts / deck_request_responses rows replaced for
    each partner in the batch
  - one batch_qa_reports row
  - clients/{workspace}/exports/review_queue.csv overwritten

Stub mode: when no ANTHROPIC_API_KEY is resolvable, per-partner stub_response
dicts come from a static EMAIL_BANK keyed on partner_id. The live LLM path is
the same code path; only the stub_response source differs.

Run: uv run scripts/07_generate_emails.py --workspace clients/test_workspace --top 5
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg
from core.stage_runner import stage_run
from core.csv_export import write_review_queue
from core.db import (
    batch_qa_reports,
    deal_attributions,
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    partner_score_summaries,
    partners,
    signals,
)
from core.llm.client import MODEL_EMAIL
from core.production_guards import production_gate_for_ready_to_send
from core.similarity import first_sentence, ratio_similarity, token_set_similarity
from schemas.email_generation import EmailOutput

STAGE = "07_generate_emails"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "generate_email.txt"

# Batch QA thresholds + forbidden phrases moved to core/email/batch_qa.py
# (Refactor 7/14). Re-imported below alongside the QA functions.


# ------- strategy eligibility -------

# Strategy eligibility (compute_eligibility / pick_strategies /
# market_shift_axis_ids / has_metrics_oriented_signal / has_company_traction)
# lives in core/email/strategy_eligibility.py (Refactor item 7 / 14). Re-
# exported here so any external importer of this module's symbols keeps
# working during the extraction window.
from core.email.strategy_eligibility import (  # noqa: E402
    METRICS_SIGNAL_KEYWORDS,
    MARKET_SHIFT_AXIS_TOKENS,
    STRATEGY_TIE_BREAK,
    compute_eligibility,
    has_company_traction,
    has_metrics_oriented_signal,
    market_shift_axis_ids,
    pick_strategies,
)


# Batch 24 (#420): keyword matching against thesis text used substring
# `kw in thesis` which produces false positives like "ai" in "stairwell"
# or "art" in "smart". Use word-boundary matching against tokenized
# thesis, handling multi-word sectors ("design partners") by matching
# the phrase as a substring AFTER ensuring the surrounding chars are
# non-word.
import re as _re

_NONWORD_RE = _re.compile(r"\W+")


def _company_primary_domain(company_cfg: dict) -> str | None:
    """Best-effort: extract the company's primary domain from the
    scheduling link (post-redirect host) or fall back to the founder
    email's domain. Returns lowercase host, no port. Used by Stage 7's
    founder-email-alignment check (Batch 37 #35).

    When the scheduling link points at a third-party scheduling service
    (cal.com, calendly.com, etc.) OR an RFC 2606 reserved TLD (.example),
    the link doesn't carry the company's primary domain -- fall through
    to the founder email's domain instead.
    """
    co = (company_cfg or {}).get("company") or {}
    link = (co.get("meeting_ask") or {}).get("preferred_scheduling_link") or ""
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


def _word_boundary_hit(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` bounded by non-word chars
    (or start/end of string). Case-insensitive on the caller side; both
    inputs should already be lower()."""
    if not needle:
        return False
    pattern = _re.compile(
        r"(?:^|\W)" + _re.escape(needle) + r"(?:$|\W)"
    )
    return bool(pattern.search(haystack))


# ------- stub email bank (fixture path) -------
# Bank + build_stub_response moved to core/email/stub_bank.py
# (Refactor 14). Re-exported so any external importer keeps working.
from core.email.stub_bank import (  # noqa: E402,F401
    DECK_RESPONSE_TEMPLATE,
    EMAIL_BANK,
    build_stub_response,
)


# ------- live LLM prompt assembly (built but exercised only when key present) -------

# Live-prompt assembly moved to core/email/prompt.py (Refactor item 14).
# Re-exported here so any external importer of the symbols keeps working.
from core.email.prompt import (  # noqa: E402,F401
    _meeting_slot,
    _read_example_files,
    meeting_slot,
    read_example_files,
)
from core.email.prompt import build_live_prompt as _build_live_prompt_impl


def build_live_prompt(*, company_cfg, partner_name, fund_name, partner_bio,
                      composite_score, round_fit_score, round_fit_reasoning,
                      lead_likelihood_score, axes_summary, fund_kill_signals,
                      signals_for_partner, deals_for_partner,
                      examples_dir) -> str:
    """Thin wrapper: reads the prompt template from PROMPT_PATH and
    forwards everything else to core.email.prompt.build_live_prompt
    so that function stays pure (no side-effecting file reads) and is
    unit-testable from any fixture template string."""
    return _build_live_prompt_impl(
        prompt_template=PROMPT_PATH.read_text(encoding="utf-8"),
        company_cfg=company_cfg,
        partner_name=partner_name,
        fund_name=fund_name,
        partner_bio=partner_bio,
        composite_score=composite_score,
        round_fit_score=round_fit_score,
        round_fit_reasoning=round_fit_reasoning,
        lead_likelihood_score=lead_likelihood_score,
        axes_summary=axes_summary,
        fund_kill_signals=fund_kill_signals,
        signals_for_partner=signals_for_partner,
        deals_for_partner=deals_for_partner,
        examples_dir=examples_dir,
    )


# ------- batch QA -------
# check_hard_gates / template_smell_judge / evaluate_batch + the
# threshold constants moved to core/email/batch_qa.py (Refactor 7/14).
# Re-export the symbols so any external caller keeps working.
from core.email.batch_qa import (  # noqa: E402,F401
    _RAISE_RE,
    SIM_BODY_HARD,
    SIM_FIRST_HARD,
    SIM_SUBJECT_HARD,
    SMELL_HIGH_BODY_SIM,
    SMELL_MEDIUM_BODY_SIM,
    SMELL_TOO_SIMILAR_SIM,
    SMELL_MASS_FIRST_SIM,
    WARN_STRATEGY_SHARE,
    WARN_TEMPLATE_LOW_SHARE,
    check_hard_gates,
    evaluate_batch,
    template_smell_judge,
)


# ------- main -------

def _now() -> datetime:
    return datetime.now(timezone.utc)


READY_TO_SEND_DAILY_CEILING = 25
# Brief Gate 5.5: before scaling beyond mid-priority into top-25, a Green
# calibration cohort must exist within the last 60 days. --skip-calibration
# --reason "..." overrides for calibration runs themselves and emergencies.
TOP_BEFORE_CALIBRATION_REQUIRED = 10
CALIBRATION_WINDOW_DAYS = 60

# Batch 17 (#363/#364/#365): refuse Stage 7 when the dependency stages
# are stale relative to each other. STALE_STAGE6_HOURS bounds Stage 6
# freshness; STAGE_DEPENDENCIES enforces "Stage Y must run AFTER Stage X"
# so a re-run of Stage 4 followed directly by Stage 7 (without re-running
# Stage 5 verification + Stage 6 scoring) gets refused.
STALE_STAGE6_HOURS = 24
STAGE_DEPENDENCIES = (
    # (downstream, upstream) -- downstream must have completed after upstream
    ("05_verify_and_quality", "04_mine_partner_signals"),
    ("06_score_candidates", "05_verify_and_quality"),
    ("06_score_candidates", "03_mine_activity"),
)


def _check_stage_freshness(engine) -> list[str]:
    """Return human-readable reasons Stage 7 should refuse to run.

    Empty list means upstream stages are in a consistent, recent state.
    Each reason names the specific stage problem so the operator can
    re-run the right script.
    """
    from datetime import timedelta as _td
    from core.db import runs as _runs
    from sqlalchemy import desc as _desc, select as _select
    problems: list[str] = []

    def _latest_completed(stage: str):
        with engine.begin() as conn:
            row = conn.execute(
                _select(_runs.c.completed_at, _runs.c.records_failed)
                .where(_runs.c.stage == stage,
                       _runs.c.completed_at.isnot(None))
                .order_by(_desc(_runs.c.run_id)).limit(1)
            ).first()
        return row

    s6 = _latest_completed("06_score_candidates")
    if s6 is None:
        problems.append("Stage 6 has never completed")
    else:
        if (s6.records_failed or 0) > 0:
            problems.append(
                f"Stage 6 last run had records_failed={s6.records_failed}; "
                f"fix and re-run scripts/06_score_candidates.py first"
            )
        # SQLite stores naive datetimes; compare both sides naively.
        age_hours = (
            datetime.now(timezone.utc).replace(tzinfo=None) - s6.completed_at
        ).total_seconds() / 3600.0
        if age_hours > STALE_STAGE6_HOURS:
            problems.append(
                f"Stage 6 last completed {age_hours:.1f}h ago "
                f"(threshold {STALE_STAGE6_HOURS}h); re-run "
                f"scripts/06_score_candidates.py"
            )

    for downstream, upstream in STAGE_DEPENDENCIES:
        d = _latest_completed(downstream)
        u = _latest_completed(upstream)
        if d is None or u is None:
            # Already covered by "never completed" check above (or upstream
            # not yet run, which Stage 7 would surface via empty results).
            continue
        if d.completed_at < u.completed_at:
            problems.append(
                f"{downstream} (last completed {d.completed_at}) is "
                f"OLDER than its upstream {upstream} "
                f"(last completed {u.completed_at}); re-run "
                f"scripts/{downstream}.py"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 email generation + CSV write.")
    add_workspace_arg(parser)
    parser.add_argument("--top", type=int, default=25,
                        help="Top-N partners by send_now_priority (Gate 5 uses 5).")
    parser.add_argument(
        "--approve-bulk-ready", action="store_true",
        help="Required to mark more than 25 partners as ready_to_send in a "
             "single run (Brief Rule 16 hard ceiling). Requires --reason.",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true",
        help="Bypass the Gate 5.5 calibration check (you scaled before having "
             "a Green calibration in the last 60 days). Requires --reason.",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Required with --approve-bulk-ready or --skip-calibration.",
    )
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit RFC 2606 reserved domains (.example/.test/.invalid) "
             "in scheduling links, founder email, and partner email when "
             "deciding ready_to_send. Use for fixture / smoke-test runs "
             "ONLY; production workspaces should configure real domains.",
    )
    # Batch 17 #363/#364/#365.
    parser.add_argument(
        "--skip-freshness-check", action="store_true",
        help="Skip the stage-freshness preflight (Batch 17). Use only "
             "when you knowingly want to regenerate emails against a "
             "stale Stage 6. Requires --reason.",
    )
    args = parser.parse_args()
    if (args.approve_bulk_ready or args.skip_calibration
        or args.skip_freshness_check) and not args.reason:
        parser.error(
            "--approve-bulk-ready / --skip-calibration / "
            "--skip-freshness-check require --reason \"...\""
        )

    # Refactor sweep: stage_run() boilerplate collapse. Examples anchor
    # the LIVE prompt; stub mode skips the LLM so we only require
    # example files when ANTHROPIC_API_KEY is resolvable. Workspace .env
    # may carry the key, so peek the workspace before stage_run() decides
    # which preflight checks to run.
    from core.config_loader import load_workspace as _peek_ws
    _require_examples = bool(_peek_ws(args.workspace).env("ANTHROPIC_API_KEY"))
    with stage_run(args, stage=STAGE, require_examples=_require_examples) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm
        # WorkspacePolicy centralizes mode-driven defaults (item 10).
        from core.workspace_policy import WorkspacePolicy
        policy = WorkspacePolicy.from_workspace_and_args(ws, args)
        batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        banned = (ws.company.get("founder_voice") or {}).get("banned_phrases", []) or []
        target_sectors = {
            s.lower()
            for s in (ws.company.get("company") or {}).get("target_sectors", []) or []
        }
        market_shift_axes = market_shift_axis_ids(ws.axes)

        # ---- pull top-N partners + their context ----
        # Stage 7 generates outreach drafts; it should only operate on
        # partners that Stage 6 said are recommended_to_send. Previously
        # the query selected top-N by send_now_priority with NO
        # recommendation filter, so a partner who failed Stage 6's
        # criteria 1-9 could still get a draft generated (and consume
        # one of the --top slots). rec_in_batch only saw the filtered
        # set for the Rule 16 ceiling gate but the loop saw everyone.
        with engine.begin() as conn:
            rows = list(conn.execute(
                select(
                    partner_score_summaries,
                    partners.c.name.label("partner_name"),
                    partners.c.title,
                    partners.c.linkedin_url,
                    partners.c.warm_path_available,
                    partners.c.bio,
                    # Slice 1: cold-outreach approval needs partner email
                    # (block approval when missing) and do_not_contact
                    # (hard block) in the routing decision.
                    partners.c.email.label("partner_email"),
                    partners.c.do_not_contact,
                    # Slice 7: relationship suppression inputs.
                    partners.c.relationship_status,
                    partners.c.last_contacted_at,
                    partners.c.last_reply_at,
                    funds.c.name.label("fund_name"),
                    funds.c.domain.label("fund_domain"),
                    funds.c.stated_thesis,
                    # Finding 5: surface fund-level kill signals into the live
                    # prompt so the LLM can avoid triggering them.
                    funds.c.kill_signals.label("fund_kill_signals"),
                )
                .join(partners, partners.c.partner_id == partner_score_summaries.c.partner_id)
                .join(funds, funds.c.fund_id == partners.c.fund_id)
                .where(partner_score_summaries.c.recommended_to_send.is_(True))
                .order_by(partner_score_summaries.c.send_now_priority.desc())
                .limit(args.top)
            ))
            # Per-partner per-axis scores -- consumed by the live prompt's
            # TOP_AXES_NAMES_AND_SCORES placeholder so the LLM strategy picker
            # has the deterministic axis ranking in hand.
            from core.db import scores as _scores
            axis_scores_by_partner: dict[str, list[tuple[str, float | None]]] = {}
            for s in conn.execute(select(_scores)):
                axis_scores_by_partner.setdefault(s.partner_id, []).append(
                    (s.axis_id, s.score)
                )
            # Per-partner: verified quality>=2 signals.
            signals_by_partner: dict[str, list[dict]] = {}
            for s in conn.execute(
                select(signals).where(
                    signals.c.verified.is_(True),
                    signals.c.signal_quality_score >= 2,
                )
            ):
                signals_by_partner.setdefault(s.partner_id, []).append({
                    "id": int(s.signal_id),
                    "quote": s.quoted_text,
                    "source_url": s.source_url,
                    "source_type": s.source_type,
                    "axes": json.loads(s.axis_relevance or "[]"),
                    "direction": s.signal_direction,
                    "quality": int(s.signal_quality_score),
                    "date": s.quote_date,
                })
            # Partner-attributed deals.
            deals_by_partner: dict[str, list[dict]] = {}
            for d in conn.execute(select(deal_attributions)):
                if d.attributed_partner_id:
                    deals_by_partner.setdefault(d.attributed_partner_id, []).append({
                        "company": d.company,
                        "round_type": d.round_type,
                        "round_size_usd": d.round_size_usd,
                        "announcement_date": d.announcement_date,
                        "lead_fund_id": d.lead_fund_id,
                    })

        # Rows are already filtered to recommended_to_send=True by the
        # query above; this list is kept for the ceiling gate's intent
        # (count of partners that would land as ready_to_send).
        rec_in_batch = rows

        # Both safety refusals (Gate 5.5 calibration + Rule 16 ceiling) now
        # live inside the RunLogger context so the refusal lands in `runs`
        # with run.failed=1 and an audit note. The previous early-returns
        # produced no run row -- the most important refusals were invisible
        # to status.py / audit.

        # Batch 17 (#363/#364/#365): stage-freshness preflight.
        if not args.skip_freshness_check:
            fresh_problems = _check_stage_freshness(engine)
            if fresh_problems:
                msg = (
                    "FRESHNESS REFUSED: "
                    + "; ".join(fresh_problems)
                    + " (re-run the upstream stage, OR pass "
                      "--skip-freshness-check --reason '...')"
                )
                print(f"[stage 7] {msg}")
                run.note(msg)
                run.failed = max(run.failed, 1)
                return 2
        elif args.skip_freshness_check:
            run.note(
                f"FRESHNESS_SKIPPED reason={args.reason!r}"
            )

        # Gate 5.5: scaling beyond mid-tier without a recent Green cal.
        if args.top > TOP_BEFORE_CALIBRATION_REQUIRED and not args.skip_calibration:
            from datetime import timedelta as _td
            from core.db import calibration_cohorts as _cc
            from sqlalchemy import select as _select, desc as _desc
            cutoff = datetime.now(timezone.utc) - _td(days=CALIBRATION_WINDOW_DAYS)
            with engine.begin() as conn:
                green = conn.execute(
                    _select(_cc).where(
                        _cc.c.outcome == "green",
                        _cc.c.completed_at >= cutoff,
                    ).order_by(_desc(_cc.c.completed_at)).limit(1)
                ).first()
            if not green:
                msg = (
                    f"GATE 5.5 REFUSED: --top={args.top} > "
                    f"{TOP_BEFORE_CALIBRATION_REQUIRED} requires a Green "
                    f"calibration cohort within the last "
                    f"{CALIBRATION_WINDOW_DAYS} days; none found. Run "
                    f"scripts/calibration.py --start, or pass "
                    f"--skip-calibration --reason \"...\"."
                )
                print(f"[stage 7] {msg}")
                run.note(msg)
                run.failed = 1
                return 2

        # Rule 16 hard ceiling.
        if (
            len(rec_in_batch) > READY_TO_SEND_DAILY_CEILING
            and not args.approve_bulk_ready
        ):
            msg = (
                f"HARD CEILING REFUSED: {len(rec_in_batch)} partners would "
                f"be marked ready_to_send (> {READY_TO_SEND_DAILY_CEILING}). "
                f"Re-run with --approve-bulk-ready --reason \"...\"."
            )
            print(f"[stage 7] {msg}")
            run.note(msg)
            run.failed = 1
            return 2
        if args.approve_bulk_ready:
            # Log the approval into the runs row's audit summary (Criterion 15).
            run.note(
                f"BULK_READY_APPROVED count={len(rec_in_batch)} "
                f"reason={args.reason!r}"
            )
            print(
                f"[stage 7] bulk-ready approved by user: "
                f"{len(rec_in_batch)} records / reason={args.reason!r}"
            )
        if args.skip_calibration:
            run.note(f"CALIBRATION_SKIPPED reason={args.reason!r}")
            print(
                f"[stage 7] calibration check skipped by user: "
                f"reason={args.reason!r}"
            )
        recommended_drafts: list[dict] = []
        all_drafts: list[dict] = []
        partner_outputs: list[tuple[dict, EmailOutput, list[str]]] = []

        for row in rows:
            with run.attempt():
                partner_id = row.partner_id
                try:
                    p_signals = signals_by_partner.get(partner_id, [])
                    p_deals = deals_by_partner.get(partner_id, [])

                    # ---- strategy eligibility ----
                    # signal_led / contrarian_thesis_led need a partner quote
                    # the email can riff on positively. A negative-direction
                    # quote ("regulation kills startups") at quality=3 does NOT
                    # unlock signal_led -- it's evidence of MISFIT, not signal.
                    positive_signals = [
                        s for s in p_signals
                        if (s.get("direction") or "").lower() == "positive"
                    ]
                    has_q3 = any(s["quality"] >= 3 for s in positive_signals)
                    has_q2 = any(s["quality"] >= 2 for s in positive_signals)
                    # Loose single-keyword match is too generous (e.g. "infrastructure"
                    # matches both Foundry-style climate-infra and Northbeam-style
                    # fintech-infra). Require >=2 target-sector keyword hits.
                    # Batch 24 (#420): substring matching causes "art" -> "smart"
                    # and "ai" -> "stairwell" false positives. Use word-
                    # boundary matching against tokenized thesis so multi-word
                    # sectors like "design partners" still hit correctly.
                    thesis_lower = (row.stated_thesis or "").lower()
                    fund_adjacent = sum(
                        1 for kw in target_sectors
                        if kw and _word_boundary_hit(thesis_lower, kw)
                    ) >= 2
                    # partner_led_in_target: partner has a named-lead deal at a fund
                    # whose thesis is target-adjacent.
                    partner_led_in_target = bool(p_deals) and fund_adjacent
                    # market_shift_led eligibility: partner has POSITIVE-direction
                    # signal tagged with an axis whose name/description signals
                    # timing-driven category conviction (resolved from axes.yaml;
                    # previously hardcoded to axis_4 and ignored direction).
                    market_window_match = bool(market_shift_axes) and any(
                        set(s.get("axes") or []) & market_shift_axes
                        for s in positive_signals
                    )
                    # Finding 11: traction_led requires BOTH the company having
                    # current traction in config AND THIS partner having a
                    # metrics-oriented signal in their quoted_text. Previously
                    # this was hardcoded True, which would have flagged
                    # traction_led for partners with no metric vocabulary in
                    # their public signal.
                    company_traction_proof = (
                        has_company_traction(ws.company)
                        and has_metrics_oriented_signal(p_signals)
                    )

                    elig = compute_eligibility(
                        has_q3=has_q3,
                        has_q2=has_q2,
                        fund_adjacent=fund_adjacent,
                        partner_led_in_target=partner_led_in_target,
                        market_window_match=market_window_match,
                        company_traction_proof=company_traction_proof,
                    )
                    strategies = pick_strategies(elig)
                    if not strategies:
                        run.skip()
                        run.log_error(
                            partner_id, "no_eligible_strategies",
                            f"strategy eligibility: {elig}"
                        )
                        continue

                    # ---- generate (live LLM or stub) ----
                    stub = build_stub_response(partner_id, strategies)
                    # Stub mode (no ANTHROPIC_API_KEY): EMAIL_BANK miss means we
                    # can't produce variants offline. WARN + skip (Brief Rule 14:
                    # no silent failures), count as skipped not succeeded.
                    # Live mode: stub being None is fine -- the LLM runs against
                    # prompts/generate_email.txt and stub_response is unused.
                    if stub is None and llm.stub:
                        print(
                            f"[stage 7] WARN: partner {partner_id} "
                            f"({row.partner_name}) has no entry in stub "
                            "EMAIL_BANK; no variants generated. A live LLM "
                            "would handle this partner via prompts/generate_email.txt."
                        )
                        run.skip()
                        run.log_error(
                            partner_id, "stub_bank_miss",
                            "stub mode: no EMAIL_BANK entry for this partner",
                        )
                        continue
                    # Live mode would build the full prompt; in stub mode the
                    # client validates the stub directly. Finding 5: surface
                    # composite/round_fit/lead_likelihood scores, per-axis
                    # summary, and fund kill_signals into the prompt so the
                    # live LLM strategy picker is properly grounded.
                    axes_for_p = sorted(
                        axis_scores_by_partner.get(partner_id, []),
                        key=lambda a: -(a[1] or 0),
                    )
                    axes_summary = ", ".join(
                        f"{ax_id} ({score:.1f})" for ax_id, score in axes_for_p
                        if score is not None
                    )
                    prompt = build_live_prompt(
                        company_cfg=ws.company,
                        partner_name=row.partner_name,
                        fund_name=row.fund_name,
                        partner_bio=getattr(row, "bio", None),
                        composite_score=getattr(row, "composite_fit_score", None),
                        round_fit_score=getattr(row, "round_fit_score", None),
                        round_fit_reasoning=getattr(row, "round_fit_reasoning", None),
                        lead_likelihood_score=getattr(row, "lead_likelihood_score", None),
                        axes_summary=axes_summary,
                        fund_kill_signals=getattr(row, "fund_kill_signals", None),
                        signals_for_partner=p_signals,
                        deals_for_partner=p_deals,
                        examples_dir=ws.examples_dir,
                    )
                    output: EmailOutput = llm.complete_json(
                        prompt=prompt,
                        schema=EmailOutput,
                        model=MODEL_EMAIL,
                        stub_response=stub,
                    )

                    # Track drafts for batch QA.
                    draft_recs: list[str] = []
                    for v in output.variants:
                        is_rec = (v.strategy == output.recommended_variant_strategy)
                        draft_recs.append(v.strategy)
                        rec = {
                            "partner_id": partner_id,
                            "partner_name": row.partner_name,
                            "strategy": v.strategy,
                            "subject": v.subject,
                            "body": v.body,
                            "is_recommended": is_rec,
                        }
                        all_drafts.append(rec)
                        if is_rec:
                            recommended_drafts.append(rec)
                    partner_outputs.append((dict(row._mapping), output, draft_recs))
                except Exception as exc:  # noqa: BLE001
                    run.fail(partner_id, type(exc).__name__, str(exc))

        # ---- batch QA ----
        qa = evaluate_batch(recommended_drafts, all_drafts)

        # ---- batch QA hard gate ----
        # If batch QA failed (similarity dupes, template_smell=high in any
        # draft, or missing raise references), refuse to publish. We still
        # record an audit row in batch_qa_reports so the operator can see
        # WHICH batch failed and WHY, but:
        #   - no new email_drafts/followup_drafts/deck_request_responses
        #     are inserted (prior good batch survives intact)
        #   - review_queue.csv is NOT overwritten (last good CSV stays)
        #   - run.failed=1 + the reasons land in runs.error_summary
        if not qa["passed"]:
            with engine.begin() as conn:
                conn.execute(batch_qa_reports.insert().values(
                    batch_id=batch_id,
                    batch_size=len(all_drafts),
                    strategy_distribution=json.dumps(qa["strategy_distribution"]),
                    similarity_failures=qa["similarity_failure_count"],
                    template_smell_high_count=qa["template_smell_high_count"],
                    raise_reference_missing_count=qa["raise_reference_missing_count"],
                    passed=False,
                    failure_reasons=json.dumps(
                        qa["hard_fail_reasons"] + qa["warnings"]
                    ),
                    generated_at=_now(),
                ))
            msg = (
                f"BATCH QA REFUSED: {len(qa['hard_fail_reasons'])} hard fail "
                f"reason(s); prior review_queue.csv and email_drafts left "
                f"intact. Reasons: {'; '.join(qa['hard_fail_reasons'])}"
            )
            print(f"[stage 7] {msg}")
            for hf in qa["hard_fail_reasons"]:
                print(f"[stage 7] HARD FAIL: {hf}")
            for w in qa["warnings"]:
                print(f"[stage 7] WARN: {w}")
            run.note(msg)
            run.failed = max(run.failed, 1)
            return 2

        # ---- persistence ----
        # Stale-state invalidation (Findings 11, 115, 116):
        # Only delete prior drafts for partners we ACTUALLY generated new
        # output for. If LLM generation crashed for partner X mid-batch,
        # X's old drafts must not be wiped without replacement -- otherwise
        # a flaky LLM run silently erases the operator's prior usable
        # batch for those partners.
        # Batch 37 (#38): additionally PRESERVE prior drafts when this
        # run's RECOMMENDED draft for the same partner has per-draft
        # hard-gate failures. Otherwise a bad regeneration would
        # silently replace a good prior draft. The partner's downgrade
        # to outreach_status=draft already lands in the CSV reasoning;
        # this just refuses to nuke the historical email_drafts row.
        partner_ids_with_failed_rec: set[str] = set()
        for pctx, output, _ in partner_outputs:
            rec_strategy = output.recommended_variant_strategy
            if not rec_strategy:
                continue
            for v in output.variants:
                if v.strategy != rec_strategy:
                    continue
                hf = check_hard_gates(
                    {"subject": v.subject, "body": v.body}, banned,
                )
                if hf:
                    partner_ids_with_failed_rec.add(pctx["partner_id"])
                break
        partner_ids_in_batch = [
            pctx["partner_id"] for pctx, _output, _ in partner_outputs
            if pctx["partner_id"] not in partner_ids_with_failed_rec
        ]
        if partner_ids_with_failed_rec:
            run.note(
                f"PRESERVED prior drafts for "
                f"{len(partner_ids_with_failed_rec)} partner(s) whose new "
                f"recommended draft failed per-draft hard gates: "
                f"{sorted(partner_ids_with_failed_rec)} (Batch 37 #38)"
            )
        with engine.begin() as conn:
            for pid in partner_ids_in_batch:
                conn.execute(delete(email_drafts).where(email_drafts.c.partner_id == pid))
                conn.execute(delete(followup_drafts).where(followup_drafts.c.partner_id == pid))
                conn.execute(delete(deck_request_responses).where(
                    deck_request_responses.c.partner_id == pid))

            # Index per-draft QA results by (partner_id, strategy).
            qa_by_key = {
                (d["partner_id"], d["strategy"]): d for d in all_drafts
            }
            for pctx, output, _ in partner_outputs:
                pid = pctx["partner_id"]
                # Launch-blocker fix: previously we excluded
                # failed-regeneration partners from the DELETE but still
                # INSERTED their failed new drafts -- the prior good
                # draft survived but the bad draft became the newest
                # recommended row, which Stage 8's "latest draft wins"
                # ordering would pick up. Skip the insert too so the
                # preservation guarantee is actually airtight.
                if pid in partner_ids_with_failed_rec:
                    continue
                # Slice 1: every new draft starts in the approval state
                # machine as `needs_review`. Stage 7 produces drafts but
                # only a HUMAN can move them to approved_to_send.
                from core.approval.persistence import (
                    compute_draft_hash, seed_draft,
                )
                # Slice 11: build the founder-conviction-to-partner
                # bridge once per partner; every variant for that
                # partner shares the same bridge (same founder belief,
                # same supporting signal).
                from core.email.founder_conviction import (
                    build_bridge, founder_conviction_from_company,
                )
                fc = founder_conviction_from_company(ws.company)
                bridge = build_bridge(
                    founder_conviction=fc,
                    partner_signals=signals_by_partner.get(pid, []),
                )
                for v in output.variants:
                    is_rec = (v.strategy == output.recommended_variant_strategy)
                    smell_info = qa_by_key.get((pid, v.strategy), {})
                    hard_fails = check_hard_gates(
                        {"subject": v.subject, "body": v.body}, banned
                    )
                    qa_status = "pass" if not hard_fails and smell_info.get("template_smell") != "high" else "fail"
                    draft_hash_value = compute_draft_hash(v.subject, v.body)
                    # Batch 23 (#471/#472): leave written_to_csv_at NULL
                    # here. It's set AFTER write_review_queue() returns
                    # successfully, so a CSV failure no longer claims the
                    # rows landed in the CSV.
                    result = conn.execute(email_drafts.insert().values(
                        partner_id=pid,
                        batch_id=batch_id,
                        strategy=v.strategy,
                        subject=v.subject,
                        body=v.body,
                        conversion_hypothesis=v.conversion_hypothesis,
                        likely_objection=v.likely_objection,
                        objection_preempted=v.objection_preempted,
                        preemption_line=v.preemption_line,
                        template_smell=smell_info.get("template_smell", "unscored"),
                        qa_status=qa_status,
                        regeneration_count=0,
                        is_recommended=is_rec,
                        generated_at=_now(),
                        written_to_csv_at=None,
                        approval_status="needs_review",
                        draft_hash=draft_hash_value,
                        bridge_founder_claim=(
                            bridge.founder_claim if bridge else None
                        ),
                        bridge_partner_belief=(
                            bridge.partner_belief if bridge else None
                        ),
                        bridge_partner_evidence=(
                            bridge.partner_evidence if bridge else None
                        ),
                        bridge_sentence=(
                            bridge.bridge_sentence if bridge else None
                        ),
                        bridge_factual_risk=(
                            bridge.factual_risk if bridge else None
                        ),
                        bridge_confidence=(
                            bridge.confidence if bridge else None
                        ),
                    ))
                    new_draft_id = int(result.inserted_primary_key[0])
                    # Write the initial needs_review event in
                    # draft_approvals so the audit trail is intact even
                    # if the operator never touches the draft. seed_draft
                    # is idempotent so concurrent runs don't duplicate.
                    # Pass `conn` (not engine) so the event INSERT lands
                    # inside the same transaction -- SQLite deadlocks on
                    # nested engine.begin() blocks.
                    seed_draft(
                        conn,
                        draft_id=new_draft_id,
                        partner_id=pid,
                        draft_hash=draft_hash_value,
                        actor="system",
                        notes=f"stage_7 generated; qa_status={qa_status}",
                    )
                # Batch 23 (#473/#474): tag followup + deck with batch_id
                # so they can be reconciled to email_drafts.batch_id later.
                conn.execute(followup_drafts.insert().values(
                    partner_id=pid, batch_id=batch_id,
                    body=output.followup_draft,
                    generated_at=_now(),
                ))
                conn.execute(deck_request_responses.insert().values(
                    partner_id=pid, batch_id=batch_id,
                    body=output.deck_request_response,
                    generated_at=_now(),
                ))
            conn.execute(batch_qa_reports.insert().values(
                batch_id=batch_id,
                batch_size=len(all_drafts),
                # Batch 23 (#367/#467): partner count alongside the draft
                # count so the operator can reconcile both.
                batch_partner_count=len({d["partner_id"] for d in all_drafts}),
                strategy_distribution=json.dumps(qa["strategy_distribution"]),
                similarity_failures=qa["similarity_failure_count"],
                template_smell_high_count=qa["template_smell_high_count"],
                raise_reference_missing_count=qa["raise_reference_missing_count"],
                passed=qa["passed"],
                failure_reasons=json.dumps(qa["hard_fail_reasons"] + qa["warnings"]),
                generated_at=_now(),
            ))

        # ---- CSV write ----
        rec_by_partner = {
            d["partner_id"]: d for d in all_drafts if d.get("is_recommended")
        }
        alt_by_partner: dict[str, dict] = {}
        for d in all_drafts:
            if not d.get("is_recommended"):
                alt_by_partner[d["partner_id"]] = d

        # Pre-compute the set of partners whose recommended draft is in a
        # similarity-failure pair (Finding 1: don't mark such drafts
        # ready_to_send just because Stage 6 said so).
        sim_failed_partners: set[str] = set()
        for pid_a, pid_b, _kind, _score in qa["similarity_failures"]:
            sim_failed_partners.add(pid_a)
            sim_failed_partners.add(pid_b)

        # Build CSV rows.
        csv_rows: list[dict] = []
        downgraded_count = 0
        for pctx, output, _ in partner_outputs:
            pid = pctx["partner_id"]
            rec = rec_by_partner.get(pid)
            alt = alt_by_partner.get(pid)
            if not rec:
                continue
            p_signals = signals_by_partner.get(pid, [])
            top_signals_str = "\n".join(
                f'"{s["quote"]}" - {s["source_url"]} ({s["date"]})'
                for s in sorted(
                    p_signals, key=lambda s: s["quality"], reverse=True
                )[:3]
            )
            base = {
                "partner_id": pid,
                "partner_name": pctx["partner_name"],
                "partner_title": pctx["title"],
                "fund_name": pctx["fund_name"],
                "fund_domain": pctx["fund_domain"],
                "linkedin_url": pctx["linkedin_url"],
                "send_now_priority": round(pctx["send_now_priority"] or 0, 2),
                "composite_fit_score": round(pctx["composite_fit_score"] or 0, 2),
                "round_fit_score": pctx["round_fit_score"],
                "round_fit_reasoning": pctx["round_fit_reasoning"],
                "lead_likelihood_score": pctx["lead_likelihood_score"],
                "lead_likelihood_signals": pctx["lead_likelihood_signals"],
                "cold_reachability_score": pctx["cold_reachability_score"],
                "spiky_belief_score": round(pctx["spiky_belief_score"] or 0, 3),
                "top_signals": top_signals_str,
                "recommended_to_send": pctx["recommended_to_send"],
                "recommendation_reasoning": pctx["recommendation_reasoning"],
                "email_strategy_used": rec["strategy"],
                "email_subject_line": rec["subject"],
                "outreach_email_draft": rec["body"],
                "conversion_hypothesis": next(
                    v.conversion_hypothesis for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "likely_objection": next(
                    v.likely_objection for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "objection_preempted": next(
                    v.objection_preempted for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "email_alternate_strategy": alt["strategy"] if alt else "",
                "email_draft_alternate": alt["body"] if alt else "",
                "followup_email_draft": output.followup_draft,
                "deck_request_response": output.deck_request_response,
                "template_smell": rec.get("template_smell", "unscored"),
                "warm_path_available": "" if pctx["warm_path_available"] is None else bool(pctx["warm_path_available"]),
            }
            # ---- outreach_status routing (Findings 1 + 3) ----
            # Routing decision owned by core/email/draft_routing.py
            # (Refactor item 14). See that module for the full set of
            # per-draft QA checks + the warm-path / downgrade /
            # ready_to_send decision order.
            from core.email.draft_routing import decide_draft_routing
            decision = decide_draft_routing(
                rec_subject=rec.get("subject"),
                rec_body=rec.get("body"),
                rec_template_smell=rec.get("template_smell"),
                in_sim_failure_pair=pid in sim_failed_partners,
                pctx_recommendation_reasoning=pctx["recommendation_reasoning"],
                pctx_recommended_to_send=bool(pctx["recommended_to_send"]),
                # Slice 1: warm-path kwarg accepted but ignored.
                pctx_warm_path_available=None,
                pctx_cold_reachability_score=pctx.get("cold_reachability_score"),
                pctx_partner_email=pctx.get("partner_email"),
                pctx_do_not_contact=bool(pctx.get("do_not_contact") or False),
                pctx_relationship_status=pctx.get("relationship_status"),
                pctx_last_contacted_at=pctx.get("last_contacted_at"),
                pctx_last_reply_at=pctx.get("last_reply_at"),
                banned=banned,
                company_cfg=ws.company,
                allow_example_domains=policy.allow_example_domains,
            )
            base["outreach_status"] = decision.outreach_status
            base["recommendation_reasoning"] = decision.reasoning
            if decision.downgraded:
                downgraded_count += 1
            csv_rows.append(base)

        out_path = write_review_queue(ws.exports_dir, csv_rows)

        # Batch 23 (#471/#472): stamp written_to_csv_at on the recommended
        # email_drafts rows AFTER the CSV write returned successfully.
        # If write_review_queue() had raised, none of these rows would
        # claim they were written. Slice 1: every row in the review CSV
        # gets stamped -- the operator-visible status is needs_review
        # or qa_failed; either way the row landed in the CSV.
        rec_partner_ids = [
            r["partner_id"] for r in csv_rows
            if r.get("outreach_status") in ("needs_review", "qa_failed")
        ]
        if rec_partner_ids:
            now = _now()
            with engine.begin() as conn:
                conn.execute(
                    email_drafts.update()
                    .where(
                        email_drafts.c.batch_id == batch_id,
                        email_drafts.c.is_recommended.is_(True),
                        email_drafts.c.partner_id.in_(rec_partner_ids),
                    )
                    .values(written_to_csv_at=now)
                )

        # Slice 1: 'ready' now means 'in needs_review' -- nothing is
        # auto-ready_to_send. Approval is a separate human step.
        ready = sum(
            1 for r in csv_rows
            if r["outreach_status"] == "needs_review"
        )
        qa_failed = sum(
            1 for r in csv_rows if r["outreach_status"] == "qa_failed"
        )
        print(
            f"[stage 7] {len(csv_rows)} CSV row(s) -> {out_path} "
            f"(needs_review={ready}, qa_failed={qa_failed}). "
            f"NEXT: review + approve via scripts/list_pending_review.py"
        )
        if downgraded_count:
            # Finding 1: a Stage-6 recommended partner whose generated draft
            # failed QA must NOT land as ready_to_send. Surface the count
            # loudly so the operator looks at the reasoning column.
            print(
                f"[stage 7] DOWNGRADED {downgraded_count} recommended "
                f"partner(s) to draft due to per-draft QA failures "
                f"(see recommendation_reasoning column)."
            )
            run.note(f"downgraded_to_draft={downgraded_count}")
        print(
            f"[stage 7] batch QA: passed={qa['passed']} | "
            f"similarity failures={qa['similarity_failure_count']} | "
            f"template_smell=high {qa['template_smell_high_count']} | "
            f"strategy distribution={qa['strategy_distribution']}"
        )
        for w in qa["warnings"]:
            print(f"[stage 7] WARN: {w}")
        for hf in qa["hard_fail_reasons"]:
            print(f"[stage 7] HARD FAIL: {hf}")
        print(f"[stage 7] llm stub mode: {llm.stub}")

    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
