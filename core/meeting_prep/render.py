"""Markdown rendering for the two meeting-prep artifacts.

Kept separate from the builders so the JSON shape can evolve without
changing the renderer (and so the renderer is trivially unit-testable
without a live LLM)."""
from __future__ import annotations

from schemas.framing_brief import FramingBriefV1
from schemas.objection_map import ObjectionMapV1


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
