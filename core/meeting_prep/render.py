"""Markdown rendering for the two meeting-prep artifacts.

Kept separate from the builders so the JSON shape can evolve without
changing the renderer (and so the renderer is trivially unit-testable
without a live LLM)."""
from __future__ import annotations

from schemas.framing_brief import FramingBriefV1
from schemas.investor_dossier import FitVerdict, InvestorDossier
from schemas.objection_map import ObjectionMapV1


# Visual verdict tag used in the Fit Assessment table. Closed list
# matches the schema's FitVerdict literals; an unrecognized value
# (shouldn't happen post-validation) falls through to the unknown tag.
_VERDICT_TAG: dict[str, str] = {
    "strong": "STRONG",
    "neutral": "NEUTRAL",
    "weak": "WEAK",
    "unknown": "UNKNOWN",
}


def render_objection_map(obj_map: ObjectionMapV1) -> str:
    if obj_map.insufficient_evidence:
        note = obj_map.notes or "(no note)"
        return (
            "## Objections to prepare for\n"
            f"_Insufficient evidence to build a partner-specific map. {note}_\n"
        )
    lines = ["## Objections to prepare for"]
    for i, o in enumerate(obj_map.objections, 1):
        source_tag = {
            "stated_thesis": "stated thesis",
            "portfolio_pattern": "portfolio pattern",
            "public_position": "public position",
            "sector_norm": "generic sector norm",
        }.get(o.source, o.source)
        cites = ""
        if o.citing_signal_ids:
            cites = f" [signal_ids: {', '.join(str(s) for s in o.citing_signal_ids)}]"
        lines.append(
            f"{i}. **{o.objection}** "
            f"_({source_tag}, severity={o.severity}){cites}_"
        )
        lines.append(f"   - Underlying concern: {o.underlying_concern}")
        lines.append(f"   - Strong answer: {o.strong_answer_hint}")
        lines.append(f"   - Weak answer (avoid): {o.weak_answer_hint}")
    lines.append("")
    return "\n".join(lines)


def render_framing_brief(brief: FramingBriefV1) -> str:
    if brief.insufficient_evidence:
        note = brief.notes or "(no note)"
        return (
            "## How to tell your story today\n"
            f"_Insufficient evidence to build a partner-specific brief. {note}_\n"
        )
    lines = ["## How to tell your story today"]
    lines.append(f"- **Lead with:** {brief.lead_with}")
    if brief.amplify:
        lines.append("- **Amplify:**")
        for item in brief.amplify:
            lines.append(f"  - {item}")
    if brief.address_unprompted:
        lines.append("- **Address unprompted:**")
        for item in brief.address_unprompted:
            lines.append(f"  - {item}")
    if brief.do_not_lead_with:
        lines.append("- **Do not lead with:**")
        for item in brief.do_not_lead_with:
            lines.append(f"  - {item}")
    lines.append(f"- **Question to ask them:** {brief.question_to_ask_them}")
    if brief.citing_signal_ids:
        lines.append(
            "- _Citing signals: "
            f"{', '.join(str(s) for s in brief.citing_signal_ids)}_"
        )
    lines.append("")
    return "\n".join(lines)


def render_investor_dossier(d: InvestorDossier) -> str:
    """Render the full dossier in the Christo-sample style.

    Insufficient-evidence path: the renderer surfaces the structured
    evidence_gaps + the partial header so the operator sees exactly
    what's missing, rather than a half-empty pretty doc that looks
    like a real dossier.
    """
    lines: list[str] = []
    lines.append("# CONFIDENTIAL -- INVESTOR DOSSIER")
    lines.append("")
    lines.append(f"**{d.partner_name or '(unnamed partner)'}**")
    if d.partner_role or d.fund_name:
        role_fund = " - ".join(
            x for x in (d.partner_role, d.fund_name) if x
        )
        lines.append(f"_{role_fund}_")
    if d.former_roles:
        lines.append("_Former: " + "; ".join(d.former_roles) + "_")
    loc_themes = []
    if d.location:
        loc_themes.append(d.location)
    if d.investment_themes:
        loc_themes.append("Themes: " + ", ".join(d.investment_themes))
    if loc_themes:
        lines.append("_" + " | ".join(loc_themes) + "_")
    lines.append("")
    lines.append(f"_Meeting role posture: **{d.meeting_role}**_")
    lines.append("")

    if d.insufficient_evidence:
        lines.append("## Evidence gap")
        lines.append(
            "_The app has insufficient verified evidence for a full "
            "dossier. The sections below are populated where evidence "
            "exists; partner-specific topics and questions are "
            "intentionally omitted rather than fabricated._"
        )
        lines.append("")
        if d.evidence_gaps:
            lines.append("**What's missing:**")
            for gap in d.evidence_gaps:
                lines.append(f"- {gap}")
            lines.append("")

    if d.profile_summary.strip():
        lines.append("## Profile summary")
        lines.append(d.profile_summary.strip())
        lines.append("")

    _section_if_any(lines, "Who they are and how they think", [
        ("Background", d.background),
        ("Operator / investor pattern", d.operator_investor_pattern),
        ("Portfolio / advisory pattern", d.portfolio_advisory_pattern),
    ])
    _bullet_section(lines, "What they appear to value", d.what_they_value)
    _bullet_section(lines, "How to show up", d.how_to_show_up)

    firm_lines = [
        ("Founded", d.firm_founded),
        ("AUM / fund size", d.firm_aum),
        ("Stage focus", d.firm_stage_focus),
        ("Check size", d.firm_check_size),
        ("Investment model", d.firm_investment_model),
        ("LP / network", d.firm_lp_network),
        ("Recent context", d.firm_recent_context),
    ]
    if d.firm_sectors:
        firm_lines.append(("Sectors", ", ".join(d.firm_sectors)))
    _section_if_any(lines, "Firm snapshot", firm_lines)

    if d.fit_assessment:
        lines.append("## Fit assessment")
        lines.append("")
        lines.append("| Criterion | Verdict | Notes |")
        lines.append("|---|---|---|")
        for row in d.fit_assessment:
            tag = _VERDICT_TAG.get(row.verdict, row.verdict.upper())
            # Pipe-safe notes -- inline pipes break the table layout.
            notes = row.notes.replace("|", "\\|")
            lines.append(f"| {row.criterion} | {tag} | {notes} |")
        lines.append("")

    if d.lead_with_paragraph.strip() or d.what_to_emphasize or d.founder_language:
        lines.append("## Pitch framing")
        if d.lead_with_paragraph.strip():
            lines.append("**Lead with (founder voice):**")
            lines.append("")
            lines.append("> " + d.lead_with_paragraph.strip().replace("\n", "\n> "))
            lines.append("")
        if d.why_thesis_match.strip():
            lines.append(f"**Why this is a thesis match:** {d.why_thesis_match.strip()}")
            lines.append("")
        _bullet_inline(lines, "What to emphasize", d.what_to_emphasize)
        _bullet_inline(lines, "What NOT to overemphasize", d.what_not_to_overemphasize)
        if d.founder_language:
            lines.append("**Founder language to use:**")
            for phrase in d.founder_language:
                lines.append(f"- _\"{phrase}\"_")
            lines.append("")

    if d.topics_to_handle:
        lines.append("## Topics to handle carefully")
        for i, t in enumerate(d.topics_to_handle, 1):
            cites = ""
            if t.citing_signal_ids:
                cites = (
                    f" [signal_ids: "
                    f"{', '.join(str(s) for s in t.citing_signal_ids)}]"
                )
            else:
                cites = " _(generic; no partner-specific citation)_"
            lines.append(f"{i}. **{t.topic}**{cites}")
            lines.append(f"   - Why they care: {t.why_they_care}")
            lines.append(f"   - How to answer: {t.how_to_answer}")
        lines.append("")

    if d.anticipated_questions:
        lines.append("## Anticipated questions")
        for i, q in enumerate(d.anticipated_questions, 1):
            cites = ""
            if q.citing_signal_ids:
                cites = (
                    f" [signal_ids: "
                    f"{', '.join(str(s) for s in q.citing_signal_ids)}]"
                )
            lines.append(f"{i}. **{q.question}**{cites}")
            lines.append(f"   - Answer direction: {q.suggested_answer_direction}")
            lines.append(f"   - Partner-specific basis: {q.partner_specific_basis}")
        lines.append("")

    closing_pairs = [
        ("Next-step ask", d.next_step_ask),
        ("Lead vs syndicate frame", d.lead_vs_syndicate_frame),
        ("Process ask", d.process_ask),
        ("Partner-specific help to ask for", d.partner_specific_help_ask),
        ("If timing is early", d.if_too_early_framing),
    ]
    _section_if_any(lines, "Closing posture", closing_pairs)

    # Sources section ALWAYS renders (even with no citations) so the
    # operator sees the dossier's evidence basis is empty rather
    # than wondering whether it was just omitted.
    lines.append("## Sources")
    if d.citing_signal_ids:
        lines.append(
            "**Verified app signals:** "
            + ", ".join(str(s) for s in d.citing_signal_ids)
        )
    else:
        lines.append("**Verified app signals:** (none cited)")
    if d.live_research_source_urls:
        lines.append("**Live research:**")
        for url in d.live_research_source_urls:
            lines.append(f"- {url}")
    else:
        lines.append("**Live research:** (disabled or none gathered)")
    lines.append(
        f"**Style sample used for structure only:** "
        f"{'yes' if d.style_sample_used else 'no'}"
    )
    if d.notes.strip():
        lines.append("")
        lines.append(f"_Builder notes: {d.notes.strip()}_")
    lines.append("")
    return "\n".join(lines)


def _section_if_any(
    lines: list[str], title: str, pairs: list[tuple[str, str]],
) -> None:
    """Render `## title` followed by `- **label**: value` for each
    non-empty pair. Skips the whole section when every pair is
    empty, so the dossier doesn't pad out with `(empty)` headers
    when the LLM had no evidence for a section."""
    visible = [(label, val) for label, val in pairs if (val or "").strip()]
    if not visible:
        return
    lines.append(f"## {title}")
    for label, val in visible:
        lines.append(f"- **{label}:** {val}")
    lines.append("")


def _bullet_section(
    lines: list[str], title: str, items: list[str],
) -> None:
    if not items:
        return
    lines.append(f"## {title}")
    for item in items:
        lines.append(f"- {item}")
    lines.append("")


def _bullet_inline(
    lines: list[str], label: str, items: list[str],
) -> None:
    """A `**label:**` line followed by bullets. Inline within a
    larger section (e.g. Pitch framing) rather than its own heading."""
    if not items:
        return
    lines.append(f"**{label}:**")
    for item in items:
        lines.append(f"- {item}")
    lines.append("")
