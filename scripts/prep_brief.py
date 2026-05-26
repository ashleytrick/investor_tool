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
from core.meeting_prep import framing_brief as fb
from core.meeting_prep import objection_map as om
from core.meeting_prep.render import render_framing_brief, render_objection_map


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
    parser.add_argument("--partner-id", required=True)
    parser.add_argument("--out", default=None,
                        help="Write markdown to this path; default: stdout.")
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
    args = parser.parse_args()

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

    if want_objections or want_framing:
        llm = LLMClient(workspace=ws)
        if want_objections:
            obj_stub = _stub_objection_map(pid) if llm.stub else None
            obj_out = om.build(
                engine=engine, llm=llm, partner_id=pid,
                company_cfg=ws.company, force=args.force_rebuild,
                stub_response=obj_stub,
            )
            parts.append(render_objection_map(obj_out))
        if want_framing:
            obj_stub = _stub_objection_map(pid) if llm.stub else None
            fram_stub = _stub_framing_brief(pid) if llm.stub else None
            fram_out = fb.build(
                engine=engine, llm=llm, partner_id=pid,
                company_cfg=ws.company, force=args.force_rebuild,
                stub_response=fram_stub,
                objection_map_stub=obj_stub,
            )
            parts.append(render_framing_brief(fram_out))

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
