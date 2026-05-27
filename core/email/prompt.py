"""Stage 7 live-prompt assembly (Refactor item 14).

Pure functions extracted from scripts/07_generate_emails.py: read the
operator's prompts/generate_email.txt template, fill in every
workspace + per-partner placeholder, and return the final prompt
string for the LLM call.

The live-prompt path is exercised only when an ANTHROPIC_API_KEY is
resolvable; the fixture/stub path uses core/email/stub_bank.py
instead. Even so, the prompt assembly is worth its own module
because:

  - operators frequently tune the prompt template; the placeholder
    contract should live somewhere visible and testable;
  - the {TIME_1}/{TIME_2} / examples-block substitutions previously
    leaked literal `{X}` tokens into the LLM when config was missing
    (Batch 36 #6) -- the regression test for that case now sits next
    to the function rather than requiring a full Stage 7 run.
"""
from __future__ import annotations

import json
from pathlib import Path


def read_example_files(examples_dir) -> str:
    """Concatenate every prompts/examples/*.md into one block for the
    live prompt. The base prompt previously said 'load the
    corresponding example file as a style anchor', but the LLM has no
    filesystem access -- it was being told to use anchors that were
    never sent. Each file is wrapped in a header so the model can see
    which strategy it belongs to.

    Returns '(no example files available)' when the directory is
    absent or empty so the caller doesn't have to special-case it.
    """
    examples_dir = Path(examples_dir)
    if not examples_dir.exists():
        return "(no example files available)"
    chunks: list[str] = []
    for path in sorted(examples_dir.glob("*.md")):
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            continue
        chunks.append(f"--- {path.stem} ---\n{body}")
    return "\n\n".join(chunks) if chunks else "(no example files available)"


# Back-compat alias for any external importer of the private name.
_read_example_files = read_example_files


def meeting_slot(company_block: dict, idx: int) -> str:
    """Look up company.meeting_ask.preferred_time_slots[idx], falling
    back to the sentinel '(no time slot configured)' so a missing
    slot doesn't surface as a literal `{TIME_1}` placeholder in the
    final draft (which Stage 7's check_hard_gates would reject)."""
    slots = (company_block.get("meeting_ask") or {}).get(
        "preferred_time_slots"
    ) or []
    if idx < len(slots) and slots[idx]:
        return str(slots[idx])
    return "(no time slot configured)"


_meeting_slot = meeting_slot  # back-compat alias


_CHANNEL_STYLE_NOTES = {
    "email": (
        "OUTPUT FORMAT: standard cold email -- subject line + 4-sentence "
        "body. Follow the existing structure (sentence 1: opener; 2: "
        "company hook; 3: why this partner / round hook; 4: meeting "
        "ask with CTA). Signoff as the founder."
    ),
    "linkedin": (
        "OUTPUT FORMAT: LinkedIn DM -- NO subject line (emit empty "
        "string for `subject`), 3 sentences MAX, casual register, no "
        "formal signoff. The signal in sentence 1 must be short and "
        "specific. Sentence 2: one-line company description. Sentence 3: "
        "the meeting ask (\"open to 15 min next week?\" style -- NOT "
        "calendar links, LinkedIn DMs read worse with embedded URLs)."
    ),
    "both": (
        "OUTPUT FORMAT: standard cold email (see email format). The "
        "operator's channel preference is 'both' so this body will be "
        "used as the email; a separate LinkedIn DM will be generated "
        "in a follow-up pass."
    ),
}


def channel_style_note(channel: str) -> str:
    """Return the LLM-facing instruction block for a given
    channel preference. Falls back to email for any unknown value."""
    return _CHANNEL_STYLE_NOTES.get(
        (channel or "email").strip().lower(),
        _CHANNEL_STYLE_NOTES["email"],
    )


def build_live_prompt(
    *,
    prompt_template: str,
    company_cfg: dict,
    partner_name: str | None,
    fund_name: str | None,
    partner_bio: str | None,
    composite_score: float | None,
    round_fit_score: float | None,
    round_fit_reasoning: str | None,
    lead_likelihood_score: float | None,
    axes_summary: str | None,
    fund_kill_signals: str | None,
    signals_for_partner: list[dict],
    deals_for_partner: list[dict],
    examples_dir,
    operator_voice_samples: str = "",
    channel: str = "email",
) -> str:
    """Fill every placeholder in the operator's prompt template.

    `prompt_template` is the raw string Stage 7 read from
    prompts/generate_email.txt. Taking it as an argument (rather than
    re-reading the file here) keeps this function pure for testing.

    `operator_voice_samples` is the pre-formatted block from
    `web.routers.email_samples.load_voice_samples_for_prompt`. Empty
    string when the operator hasn't uploaded anything; the prompt
    template handles that gracefully by falling back to the
    `founder_voice.style` hint.

    `channel` (batch H): 'email' | 'linkedin' | 'both'. Drives the
    `{CHANNEL_STYLE_NOTE}` placeholder so the LLM emits a body shape
    suitable for the operator's chosen channel. 'linkedin' → short
    casual DM (3 sentences, no subject); 'email' → existing format;
    'both' → email-style (a parallel LinkedIn-variant pass is a
    follow-up).
    """
    c = company_cfg["company"]
    rc = company_cfg["raise_context"]
    rh = rc.get("round_hook") or {}
    secondary_metrics = c.get("current_traction", {}).get(
        "secondary_metrics", []
    )
    headline_metric = c.get("current_traction", {}).get(
        "headline_metric", ""
    )
    return (
        prompt_template
        .replace("{COMPANY_NAME}", c["name"])
        .replace("{FOUNDER_NAME}", c["founder_name"])
        .replace("{ROUND}", rc.get("round", ""))
        .replace("{RAISE_AMOUNT}", rc.get("amount", ""))
        .replace("{RAISE_STATUS}", rc.get("status", ""))
        .replace("{RAISE_TIMING}", rc.get("timing", ""))
        .replace("{WHY_THIS_ROUND_IS_FUNDABLE_NOW}",
                 rc.get("why_this_round_is_fundable_now", ""))
        .replace("{WHAT_CHANGES_AFTER_THIS_ROUND}",
                 rc.get("what_changes_after_this_round", ""))
        .replace("{ROUND_HOOK_REASON}",
                 rh.get("strongest_reason_to_meet_now", ""))
        .replace("{ROUND_HOOK_CONSEQUENCE}",
                 rh.get("investor_consequence_of_waiting", ""))
        .replace("{ROUND_HOOK_MOMENTUM_PROOF}",
                 rh.get("round_momentum_proof", ""))
        .replace("{COMPANY_DESCRIPTION}", c.get("description", ""))
        .replace("{STRONGEST_RAISE_PROOF}", rc.get("strongest_raise_proof", ""))
        .replace("{HEADLINE_METRIC}", headline_metric)
        .replace("{SECONDARY_METRICS}", ", ".join(secondary_metrics))
        .replace("{CUSTOMER_EVIDENCE}", "")
        .replace("{TECHNICAL_VALIDATION}", "")
        .replace("{NON_DILUTIVE_OR_STRATEGIC}",
                 rc.get("notable_existing_investors_or_non_dilutive", ""))
        .replace("{FOUNDER_MARKET_FIT}", "")
        .replace("{PARTNER_NAME}", partner_name or "")
        .replace("{FUND_NAME}", fund_name or "")
        .replace("{PARTNER_BIO}", partner_bio or "")
        # Finding 5: stop sending blank scoring context to the live LLM.
        .replace("{COMPOSITE_SCORE}",
                 "" if composite_score is None else f"{composite_score:.2f}")
        .replace("{ROUND_FIT_SCORE}",
                 "" if round_fit_score is None else f"{round_fit_score:.1f}")
        .replace("{LEAD_LIKELIHOOD_SCORE}",
                 ""
                 if lead_likelihood_score is None
                 else f"{lead_likelihood_score:.1f}")
        .replace("{TOP_AXES_NAMES_AND_SCORES}", axes_summary or "")
        .replace("{TOP_SIGNALS}", json.dumps([
            {"quote": s["quote"], "url": s["source_url"],
             "date": str(s.get("date"))}
            for s in signals_for_partner[:3]
        ]))
        # Stage 2 does not yet persist per-fund portfolio_companies;
        # left blank with a comment so the operator knows it's a known gap.
        .replace("{ADJACENT_PORTFOLIO_COMPANIES}", "")
        .replace("{RECENT_PARTNER_LED_DEALS}", json.dumps([
            {"company": d["company"], "round": d.get("round_type")}
            for d in deals_for_partner
        ]))
        # COMM_STYLE would need linguistic analysis we don't yet do.
        .replace("{COMM_STYLE}", "")
        .replace("{KILL_SIGNALS}", fund_kill_signals or "")
        .replace(
            "{FOUNDER_VOICE_STYLE}",
            (company_cfg.get("founder_voice") or {}).get("style", ""),
        )
        .replace("{FOUNDER_BANNED_PHRASES}", ", ".join(
            (company_cfg.get("founder_voice") or {}).get(
                "banned_phrases", [],
            )
        ))
        # Operator-uploaded voice samples (separate from the
        # per-strategy template anchors in EXAMPLES_BLOCK). Empty
        # when the operator hasn't uploaded; the template still
        # has the {FOUNDER_VOICE_STYLE} hint as a fallback.
        .replace(
            "{OPERATOR_VOICE_SAMPLES}",
            operator_voice_samples
            or "(no operator-uploaded samples yet; "
               "mirror the style hint above)",
        )
        # Batch H: channel-specific format instruction injected at
        # render time. Default 'email' keeps existing behavior;
        # 'linkedin' steers the LLM toward a short, no-subject DM.
        .replace("{CHANNEL_STYLE_NOTE}", channel_style_note(channel))
        .replace("{CHANNEL}", (channel or "email").strip().lower())
        # Inject the actual file contents AND keep the legacy
        # {EXAMPLES_DIR} token for backward-compatibility with any
        # custom prompts that still reference the directory path.
        .replace("{EXAMPLES_BLOCK}", read_example_files(examples_dir))
        .replace("{EXAMPLES_DIR}", str(examples_dir))
        .replace(
            "{MEETING_DURATION}",
            str(c.get("meeting_ask", {}).get("duration_minutes", 30)),
        )
        .replace(
            "{MEETING_FORMAT}",
            c.get("meeting_ask", {}).get("format", "video call"),
        )
        .replace(
            "{SCHEDULING_LINK}",
            c.get("meeting_ask", {}).get("preferred_scheduling_link", ""),
        )
        # Finding 6: {TIME_1}/{TIME_2} were never substituted; a live
        # LLM could emit literal placeholders. Pull from
        # company.meeting_ask.preferred_time_slots if set; else fill
        # with a sentinel. check_hard_gates ALSO rejects any leftover
        # `{...}` placeholder in the body as a belt-and-suspenders guard.
        .replace("{TIME_1}", meeting_slot(c, 0))
        .replace("{TIME_2}", meeting_slot(c, 1))
    )
