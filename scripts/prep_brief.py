"""Generate a one-page meeting prep brief for a partner.

Once a meeting is on the calendar, the system already has everything you'd
want to walk in prepared: top quotes, axis scores, conversion hypothesis,
likely objection + how to handle it, partner-led deal pattern. This script
renders all of it as markdown so the founder can read it for 5 minutes
before the call.

Run:
  uv run scripts/prep_brief.py --partner-id NAME
  uv run scripts/prep_brief.py --partner-id NAME --out ~/Desktop/prep.md
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    deal_attributions,
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    get_engine,
    outcomes,
    partner_score_summaries,
    partners,
    scores,
    signals,
)
from core.llm.client import LLMClient
from core.meeting_prep import dossier as ds
from core.meeting_prep import framing_brief as fb
from core.meeting_prep import objection_map as om
from core.meeting_prep.cache import hash_signal_set
from core.meeting_prep.drive_sync import push_if_needed
from core.meeting_prep.eligibility import (
    is_dossier_eligible,
    mark_task_resolved,
    pending_dossier_task_ids,
)
from core.meeting_prep.evidence import load_evidence
from core.meeting_prep.render import (
    render_framing_brief,
    render_investor_dossier,
    render_objection_map,
)


_AUTO_INCLUDE_STATUSES = ("replied", "meeting_booked")


def _stub_objection_map(partner_id: str) -> dict:
    """Stub response used when ANTHROPIC_API_KEY is unset (CI, tests,
    smoke runs). Returns a single sector_norm objection so the path
    exercises the renderer without requiring partner-specific
    evidence. The schema validator accepts this shape because the
    partner-specific gate only fires when source != sector_norm."""
    return {
        "partner_id": partner_id,
        "objections": [
            {
                "objection": "API concentration risk",
                "underlying_concern": (
                    "Founder may be a thin wrapper over one provider; "
                    "switching costs / negotiating leverage at risk."
                ),
                "source": "sector_norm",
                "citing_signal_ids": [],
                "strong_answer_hint": (
                    "Acknowledge the dependency, then show concrete "
                    "mitigation (multi-provider abstraction, contractual "
                    "terms, customer-facing fallback paths)."
                ),
                "weak_answer_hint": (
                    "Dismiss the risk or claim a vendor switch is "
                    "trivial without showing the architecture."
                ),
                "severity": "medium",
            },
        ],
        "insufficient_evidence": True,
        "notes": "stub-mode response (no ANTHROPIC_API_KEY)",
    }


def _record_drive(
    pushed: list, skipped: list, artifact_type: str, res,
) -> None:
    """Sort a DrivePushResult into the right footer bucket. Skipped
    pushes ARE recorded (with reason) so the operator sees why their
    brief didn't make it to Drive -- silent skips would invite
    'I clicked Connect Google -- why is nothing showing up?'"""
    if res.pushed:
        pushed.append((artifact_type, res.doc_url or "(no url)"))
    elif res.doc_id:
        # Already-pushed cache hit; show the existing url so the
        # operator can jump straight to the doc.
        pushed.append(
            (f"{artifact_type} (already on Drive)", res.doc_url or "(no url)")
        )
    else:
        skipped.append((artifact_type, res.skipped_reason or "(unknown)"))


def _stub_investor_dossier(partner_id: str) -> dict:
    """Stub dossier for offline runs. Insufficient-evidence shape so
    the schema validators don't require partner-specific content
    that a stub couldn't honestly produce."""
    return {
        "partner_id": partner_id,
        "partner_name": "(stub)",
        "partner_role": "",
        "fund_name": "(stub fund)",
        "former_roles": [],
        "location": "",
        "investment_themes": [],
        "profile_summary": "",
        "meeting_role": "unknown",
        "background": "",
        "operator_investor_pattern": "",
        "portfolio_advisory_pattern": "",
        "what_they_value": [],
        "how_to_show_up": [],
        "firm_founded": "",
        "firm_aum": "",
        "firm_stage_focus": "",
        "firm_check_size": "",
        "firm_sectors": [],
        "firm_investment_model": "",
        "firm_lp_network": "",
        "firm_recent_context": "",
        "fit_assessment": [],
        "lead_with_paragraph": "",
        "why_thesis_match": "",
        "what_to_emphasize": [],
        "what_not_to_overemphasize": [],
        "founder_language": [],
        "topics_to_handle": [],
        "anticipated_questions": [],
        "next_step_ask": "",
        "lead_vs_syndicate_frame": "",
        "process_ask": "",
        "partner_specific_help_ask": "",
        "if_too_early_framing": "",
        "citing_signal_ids": [],
        "live_research_source_urls": [],
        "style_sample_used": False,
        "insufficient_evidence": True,
        "evidence_gaps": [
            "stub-mode response (no ANTHROPIC_API_KEY); rerun with "
            "a real key to populate this dossier"
        ],
        "notes": "stub",
    }


def _render_dossier_section(
    *, engine, ws, pid: str, args, llm,
) -> tuple[str, str]:
    """Build + render the dossier for one partner. Returns
    (markdown_section, drive_footer_section). drive_footer is empty
    when --no-drive-push is set or Drive isn't connected."""
    stub = _stub_investor_dossier(pid) if llm.stub else None
    style_path = pathlib.Path(args.style_sample) if args.style_sample else None
    try:
        res = ds.build(
            engine=engine, llm=llm, partner_id=pid,
            company_cfg=ws.company,
            force_refresh=args.force_refresh,
            live_research=args.live_research,
            style_sample_path=style_path,
            stub_response=stub,
        )
    except ds.DossierIneligibleError as exc:
        # Render a clean explanatory section instead of raising --
        # ineligibility is the COMMON case (most partners are cold).
        msg = (
            "## Investor Dossier\n"
            f"_Skipped: {exc.eligibility.reason}. "
            f"Pass --force-refresh to build anyway, or wait for the "
            f"partner to reply / book a meeting._\n"
        )
        return msg, ""

    md = render_investor_dossier(res.dossier)

    # Back-fill the rendered markdown onto the artifact row we just
    # wrote so consumers don't need to re-render. Skip on cache hits
    # (the row already has its markdown stamped).
    if res.artifact_id is not None and not res.cache_hit:
        _stamp_content_markdown(engine, res.artifact_id, md)

    drive_section = ""
    if not args.no_drive_push:
        from core.meeting_prep.cache import hash_signal_set as _h
        ev = load_evidence(engine, pid)
        if ev is not None:
            sig_hash = _h(ev.quality_signal_ids)
            push_res = push_if_needed(
                engine, ws, partner_id=pid,
                signal_set_hash=sig_hash,
                artifact_type=ds.ARTIFACT_TYPE,
                markdown_text=md,
            )
            drive_section = _render_drive_footer_one(
                ds.ARTIFACT_TYPE, push_res,
            )
    return md, drive_section


def _render_drive_footer_one(artifact_type: str, res) -> str:
    """One-line Drive footer for a single artifact. Empty string
    when nothing useful to surface (silent skips would hide config
    issues from the operator)."""
    if res.pushed:
        return (
            f"## Drive sync\n"
            f"- {artifact_type}: pushed -> {res.doc_url or '(no url)'}\n"
        )
    if res.doc_id:
        return (
            f"## Drive sync\n"
            f"- {artifact_type} (already on Drive): "
            f"{res.doc_url or '(no url)'}\n"
        )
    return (
        f"## Drive sync\n"
        f"- {artifact_type}: skipped ({res.skipped_reason or 'unknown'})\n"
    )


def _stamp_content_markdown(engine, artifact_id: int, markdown: str) -> None:
    """Write rendered markdown back onto the artifact row. Separate
    from the builder write because rendering happens in the script
    layer, not in core/meeting_prep/dossier.py (which stays
    schema-only)."""
    from sqlalchemy import update
    from core.db import meeting_prep_artifacts
    with engine.begin() as conn:
        conn.execute(
            update(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.artifact_id == artifact_id
            ).values(content_markdown=markdown)
        )


def _run_pending_only(args) -> int:
    """Build a dossier for every open investor_dossier_needed task,
    write each to clients/<ws>/exports/briefs/, mark the task
    resolved with the artifact_id."""
    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="prep_brief --pending-only")
    tasks = pending_dossier_task_ids(engine)
    if not tasks:
        print("[prep_brief] no open investor_dossier_needed tasks.")
        return 0
    briefs_dir = ws.path / "exports" / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    llm = LLMClient(workspace=ws)
    succeeded = 0
    failed = 0
    for review_item_id, partner_id in tasks:
        # Mimic the single-partner args shape so _render_dossier_section
        # doesn't need a parallel implementation.
        class _A:
            pass
        a = _A()
        a.force_refresh = False
        a.live_research = args.live_research
        a.style_sample = args.style_sample
        a.no_drive_push = args.no_drive_push
        try:
            md, drive = _render_dossier_section(
                engine=engine, ws=ws, pid=partner_id, args=a, llm=llm,
            )
        except Exception as exc:  # noqa: BLE001 - report + continue
            print(f"[prep_brief] FAILED {partner_id}: {exc}")
            failed += 1
            continue
        out_path = briefs_dir / f"{partner_id}_dossier.md"
        full = md + ("\n" + drive if drive else "")
        out_path.write_text(full, encoding="utf-8")
        # Look up the artifact_id we just wrote so the resolved task
        # row points at it. We don't get it back from
        # _render_dossier_section directly (cache hits return None);
        # fetch the latest matching row by hash key.
        artifact_id = _latest_dossier_artifact_id(engine, partner_id)
        mark_task_resolved(
            engine, review_item_id=review_item_id,
            resolved_artifact_id=artifact_id,
        )
        succeeded += 1
        print(f"[prep_brief] {partner_id} -> {out_path}")
    print(
        f"[prep_brief] done: {succeeded} built, {failed} failed, "
        f"{len(tasks)} total tasks processed."
    )
    return 0 if failed == 0 else 1


def _latest_dossier_artifact_id(engine, partner_id: str) -> int | None:
    """Return the most recent investor_dossier artifact_id for the
    partner, or None if none exists yet."""
    from sqlalchemy import desc, select
    from core.db import meeting_prep_artifacts
    with engine.begin() as conn:
        row = conn.execute(
            select(meeting_prep_artifacts.c.artifact_id).where(
                meeting_prep_artifacts.c.partner_id == partner_id,
                meeting_prep_artifacts.c.artifact_type
                == ds.ARTIFACT_TYPE,
            ).order_by(desc(meeting_prep_artifacts.c.artifact_id)).limit(1)
        ).first()
    return int(row.artifact_id) if row else None


def _stub_framing_brief(partner_id: str) -> dict:
    """Stub response for the framing brief; matches the
    insufficient_evidence shape so the schema validator passes
    without requiring partner-specific citations."""
    return {
        "partner_id": partner_id,
        "lead_with": "",
        "amplify": [],
        "address_unprompted": [],
        "do_not_lead_with": [],
        "question_to_ask_them": "",
        "citing_signal_ids": [],
        "insufficient_evidence": True,
        "notes": "stub-mode response (no ANTHROPIC_API_KEY)",
    }


def _latest_outreach_status(engine, partner_id: str) -> str | None:
    """Newest outcome row's outreach_status for the partner. Returns
    None when the partner has no outcome history. The renderer uses
    this to auto-include the LLM-driven sections only when the
    partner has earned a real-world signal (reply / meeting), so
    cold-pipeline runs don't burn LLM budget."""
    with engine.begin() as conn:
        row = conn.execute(
            select(outcomes.c.outreach_status).where(
                outcomes.c.partner_id == partner_id,
            ).order_by(desc(outcomes.c.outcome_id)).limit(1)
        ).first()
    return row.outreach_status if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Meeting prep brief for a partner.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--partner-id", default=None,
        help=(
            "Partner to brief. Required UNLESS --pending-only is set "
            "(which iterates open investor_dossier_needed tasks)."
        ),
    )
    parser.add_argument("--out", default=None,
                        help="Write markdown to this path; default: stdout.")
    parser.add_argument(
        "--dossier", action="store_true",
        help=(
            "Build the full Investor Dossier (post-reply artifact) "
            "instead of just the partner-facts brief. Refuses when "
            "the partner is not dossier-eligible (no substantive "
            "reply / meeting yet) unless --force-refresh is passed."
        ),
    )
    parser.add_argument(
        "--pending-only", action="store_true",
        help=(
            "Iterate every open investor_dossier_needed review_items "
            "task, build a dossier for each partner, and mark the "
            "task resolved. Implies --dossier."
        ),
    )
    parser.add_argument(
        "--live-research", action="store_true",
        help=(
            "Allow the dossier to incorporate live research sources. "
            "(Wired into the cache key today; full fetch + citation "
            "implementation lands in a follow-up session.)"
        ),
    )
    parser.add_argument(
        "--style-sample", default=None,
        help=(
            "Path to a sample dossier (e.g. the Christo dossier .docx) "
            "used as structure/tone guidance ONLY -- claims from the "
            "sample are never copied into the output. Cache-keyed so "
            "swapping samples regenerates."
        ),
    )
    parser.add_argument(
        "--include-objections", action="store_true",
        help=(
            "Include the LLM-built objection map. "
            "Auto-enabled when outreach_status IN "
            f"{_AUTO_INCLUDE_STATUSES}; otherwise an opt-in to spend LLM "
            "budget on cold-pipeline partners."
        ),
    )
    parser.add_argument(
        "--include-framing", action="store_true",
        help=(
            "Include the LLM-built framing brief. "
            "Auto-enabled on the same statuses as --include-objections; "
            "requires the objection map (also auto-built)."
        ),
    )
    parser.add_argument(
        "--force-rebuild", action="store_true",
        help=(
            "Bypass the meeting_prep_artifacts cache even when the "
            "verified signal set is unchanged. Use after the operator "
            "manually edits a signal's verification or quality outside "
            "the normal Stage 5 path."
        ),
    )
    parser.add_argument(
        "--no-drive-push", action="store_true",
        help=(
            "Skip the auto-push to Google Drive even when the "
            "workspace has Google connected. Useful for offline runs "
            "or when iterating on the rendering layer."
        ),
    )
    parser.add_argument(
        "--force-refresh", action="store_true",
        help=(
            "Bypass the dossier cache AND the eligibility gate. Use "
            "to regenerate a dossier after a manual signal edit, or "
            "to build one for a partner who hasn't yet replied "
            "(operator opt-in)."
        ),
    )
    args = parser.parse_args()

    if args.pending_only:
        return _run_pending_only(args)
    if not args.partner_id:
        parser.error(
            "--partner-id is required unless --pending-only is set"
        )

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="prep_brief")

    pid = args.partner_id
    with engine.begin() as conn:
        partner = conn.execute(
            select(partners).where(partners.c.partner_id == pid)
        ).first()
        if not partner:
            print(f"[prep_brief] partner_id {pid!r} not found")
            return 2
        fund = conn.execute(
            select(funds).where(funds.c.fund_id == partner.fund_id)
        ).first()
        summary = conn.execute(
            select(partner_score_summaries).where(
                partner_score_summaries.c.partner_id == pid
            )
        ).first()
        # Top 3 verified quality>=2 signals.
        sigs = list(conn.execute(
            select(signals).where(
                signals.c.partner_id == pid,
                signals.c.verified.is_(True),
                signals.c.signal_quality_score >= 2,
            ).order_by(desc(signals.c.signal_quality_score),
                       desc(signals.c.quote_date)).limit(3)
        ))
        per_axis = list(conn.execute(
            select(scores).where(scores.c.partner_id == pid)
            .order_by(desc(scores.c.score))
        ))
        partner_deals = list(conn.execute(
            select(deal_attributions).where(
                deal_attributions.c.attributed_partner_id == pid
            ).order_by(desc(deal_attributions.c.announcement_date)).limit(5)
        ))
        rec = conn.execute(
            select(email_drafts).where(
                email_drafts.c.partner_id == pid,
                email_drafts.c.is_recommended.is_(True),
            ).order_by(desc(email_drafts.c.draft_id)).limit(1)
        ).first()
        # Slice 17 follow-up (#17): live (non-superseded) row only.
        followup = conn.execute(
            select(followup_drafts).where(
                followup_drafts.c.partner_id == pid,
                followup_drafts.c.superseded_at.is_(None),
            ).order_by(desc(followup_drafts.c.followup_id)).limit(1)
        ).first()
        deck = conn.execute(
            select(deck_request_responses).where(
                deck_request_responses.c.partner_id == pid,
                deck_request_responses.c.superseded_at.is_(None),
            ).order_by(desc(deck_request_responses.c.response_id)).limit(1)
        ).first()

    parts: list[str] = []
    parts.append(f"# Prep brief: {partner.name} ({fund.name if fund else '?'})")
    parts.append(f"_generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    parts.append("")

    # --- Partner facts ---
    parts.append("## Partner")
    parts.append(f"- **Name**: {partner.name}")
    if partner.title:
        parts.append(f"- **Title**: {partner.title}")
    if partner.linkedin_url:
        parts.append(f"- **LinkedIn**: {partner.linkedin_url}")
    if partner.twitter_handle:
        parts.append(f"- **Twitter**: @{partner.twitter_handle}")
    parts.append(f"- **Employment status**: {partner.employment_status}")
    if partner.warm_path_available:
        parts.append(
            f"- **Warm path available**: {partner.warm_path_contact or 'see notes'}"
        )
    parts.append("")

    # --- Fund facts ---
    if fund:
        parts.append("## Fund")
        parts.append(f"- **Name**: {fund.name}")
        if fund.stated_thesis:
            parts.append(f"- **Stated thesis**: {fund.stated_thesis}")
        if fund.stated_stage_focus:
            parts.append(f"- **Stage focus**: {fund.stated_stage_focus}")
        if fund.check_size_range:
            parts.append(f"- **Check size**: {fund.check_size_range}")
        if fund.last_known_activity_date:
            parts.append(f"- **Last known activity**: {fund.last_known_activity_date}")
        if fund.kill_signals:
            parts.append(f"- **Kill signals on file**: {fund.kill_signals}")
        parts.append("")

    # --- Scores ---
    if summary:
        parts.append("## Fit scores")
        parts.append(f"- **composite_fit_score**: {summary.composite_fit_score} / 10")
        parts.append(f"- **round_fit_score**: {summary.round_fit_score} / 10 "
                     f"({summary.round_fit_reasoning})")
        parts.append(f"- **lead_likelihood_score**: {summary.lead_likelihood_score} / 10")
        parts.append(f"- **cold_reachability_score**: {summary.cold_reachability_score} / 10")
        parts.append(f"- **send_now_priority**: {summary.send_now_priority}")
        if summary.major_kill_signal_present:
            parts.append(f"- **MAJOR KILL**: {summary.kill_signal_summary}")
        parts.append("")
        if per_axis:
            parts.append("### Per-axis scores")
            for s in per_axis:
                parts.append(f"- {s.axis_id}: **{s.score:.1f}** (confidence={s.confidence})")
            parts.append("")

    # --- Top quotes ---
    if sigs:
        parts.append("## Top verified quotes (highest quality first)")
        for s in sigs:
            try:
                axes = json.loads(s.axis_relevance or "[]")
            except json.JSONDecodeError:
                axes = []
            parts.append(
                f"- _{s.quote_date or '?'}_ ({s.source_type}, axes={axes}, "
                f"quality={s.signal_quality_score}): "
                f"\n  > {s.quoted_text}"
                f"\n  source: {s.source_url}"
            )
        parts.append("")

    # --- Partner-led deals ---
    if partner_deals:
        parts.append("## Recent deals this partner led")
        for d in partner_deals:
            tags = ""
            if d.sector_tags:
                try:
                    tags = " " + ", ".join(json.loads(d.sector_tags))
                except json.JSONDecodeError:
                    tags = ""
            size = f" ${d.round_size_usd:,}" if d.round_size_usd else ""
            parts.append(
                f"- {d.announcement_date} **{d.company}** "
                f"({d.round_type}{size}){tags}"
            )
        parts.append("")

    # --- The pitch plan ---
    if rec:
        parts.append("## What we sent (or will send)")
        parts.append(f"- **Strategy**: {rec.strategy}")
        parts.append(f"- **Subject**: {rec.subject}")
        parts.append("- **Body**:")
        for line in (rec.body or "").splitlines():
            parts.append(f"  > {line}")
        parts.append("")
        if rec.conversion_hypothesis:
            parts.append("### Why we think this converts")
            parts.append(f"{rec.conversion_hypothesis}")
            parts.append("")
        if rec.likely_objection:
            parts.append("### Most likely objection")
            parts.append(f"{rec.likely_objection}")
            if rec.objection_preempted and rec.preemption_line:
                parts.append(
                    f"_Preempted in the body by:_ \"{rec.preemption_line}\""
                )
            elif not rec.objection_preempted:
                parts.append(
                    "_Not preempted in the body. Be ready to address it live._"
                )
            parts.append("")

    # --- Reusable replies ---
    if deck:
        parts.append("## If they ask for the deck only")
        parts.append(f"> {deck.body}")
        parts.append("")
    if followup:
        parts.append("## Follow-up template (if no reply in 4-6 business days)")
        parts.append(f"> {followup.body}")
        parts.append("")

    # --- Meeting prep extensions (Build Session 12) ---
    # Auto-enable when the partner has earned a substantive signal;
    # otherwise require explicit opt-in so cold-pipeline operators
    # don't burn LLM time on partners who never replied.
    latest_status = _latest_outreach_status(engine, pid)
    auto_enable = latest_status in _AUTO_INCLUDE_STATUSES
    want_objections = args.include_objections or auto_enable
    want_framing = args.include_framing or auto_enable

    drive_results: list[tuple[str, str]] = []  # (artifact_type, url)
    drive_skipped: list[tuple[str, str]] = []  # (artifact_type, reason)
    if want_objections or want_framing:
        llm = LLMClient(workspace=ws)
        ev = load_evidence(engine, pid)
        signal_hash = (
            hash_signal_set(ev.quality_signal_ids) if ev else ""
        )
        if want_objections:
            obj_stub = _stub_objection_map(pid) if llm.stub else None
            obj_out = om.build(
                engine=engine, llm=llm, partner_id=pid,
                company_cfg=ws.company, force=args.force_rebuild,
                stub_response=obj_stub,
            )
            section = render_objection_map(obj_out)
            parts.append(section)
            if not args.no_drive_push and signal_hash:
                res = push_if_needed(
                    engine, ws, partner_id=pid,
                    signal_set_hash=signal_hash,
                    artifact_type="objection_map",
                    markdown_text=section,
                )
                _record_drive(drive_results, drive_skipped, "objection_map", res)
        if want_framing:
            obj_stub = _stub_objection_map(pid) if llm.stub else None
            fram_stub = _stub_framing_brief(pid) if llm.stub else None
            fram_out = fb.build(
                engine=engine, llm=llm, partner_id=pid,
                company_cfg=ws.company, force=args.force_rebuild,
                stub_response=fram_stub,
                objection_map_stub=obj_stub,
            )
            section = render_framing_brief(fram_out)
            parts.append(section)
            if not args.no_drive_push and signal_hash:
                res = push_if_needed(
                    engine, ws, partner_id=pid,
                    signal_set_hash=signal_hash,
                    artifact_type="framing_brief",
                    markdown_text=section,
                )
                _record_drive(drive_results, drive_skipped, "framing_brief", res)

    # Surface the Drive outcome as a footer the operator can scan
    # without scrolling back through the artifact bodies. Drive
    # status changes between runs (e.g. operator just connected
    # Google) are visible from the footer alone.
    if drive_results or drive_skipped:
        parts.append("## Drive sync")
        for atype, url in drive_results:
            parts.append(f"- {atype}: pushed -> {url}")
        for atype, reason in drive_skipped:
            parts.append(f"- {atype}: skipped ({reason})")
        parts.append("")

    # --- Investor Dossier (Build Session 14) ---
    # Post-reply artifact. Only renders when explicitly requested
    # via --dossier (or via the auto-include rule below for
    # dossier-eligible partners). Refuses for ineligible partners
    # unless --force-refresh is passed.
    if args.dossier:
        dossier_section, drive_section = _render_dossier_section(
            engine=engine, ws=ws, pid=pid, args=args, llm=LLMClient(workspace=ws),
        )
        if dossier_section:
            parts.append(dossier_section)
        if drive_section:
            parts.append(drive_section)

    # Default output path for --dossier mode points at a workspace-
    # local briefs directory so the operator can re-find them.
    # Without --dossier the original stdout behavior is preserved.
    if args.dossier and args.out is None:
        briefs_dir = ws.path / "exports" / "briefs"
        briefs_dir.mkdir(parents=True, exist_ok=True)
        args.out = str(briefs_dir / f"{pid}_dossier.md")

    output = "\n".join(parts) + "\n"
    if args.out:
        out_path = pathlib.Path(args.out)
        out_path.write_text(output, encoding="utf-8")
        print(f"[prep_brief] wrote {out_path} ({len(output.splitlines())} lines)")
    else:
        sys.stdout.write(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
