"""Batch H: channel-aware draft generation.

`partners.channel_pref` is read by Stage 7 and threaded into
`build_live_prompt` as the `channel` arg. The prompt template
renders a CHANNEL_STYLE_NOTE block that steers the LLM:

  - 'email'    → existing format (4-sentence body + subject)
  - 'linkedin' → short DM (3 sentences max, NO subject)
  - 'both'     → email-style for the primary draft; parallel
                 LinkedIn variant is a follow-up

Pre-batch-H, channel_pref persisted but Stage 7 ignored it.
"""
from __future__ import annotations

import pytest


# Minimal company_cfg fixture for build_live_prompt tests.
_CFG = {
    "company": {"name": "Acme", "founder_name": "Jane"},
    "raise_context": {"round": "seed", "amount": "$1.5M"},
    "founder_voice": {"style": "direct", "banned_phrases": []},
}

_TEMPLATE = (
    "Channel: {CHANNEL}\n"
    "{CHANNEL_STYLE_NOTE}\n"
    "\n"
    "Founder voice:\n"
    "- Style: {FOUNDER_VOICE_STYLE}\n"
    "- Banned: {FOUNDER_BANNED_PHRASES}\n"
    "Operator voice samples:\n{OPERATOR_VOICE_SAMPLES}\n"
    "{TOP_SIGNALS} {COMPOSITE_SCORE} {ROUND_FIT_SCORE} "
    "{LEAD_LIKELIHOOD_SCORE} {TOP_AXES_NAMES_AND_SCORES} "
    "{ADJACENT_PORTFOLIO_COMPANIES} {RECENT_PARTNER_LED_DEALS} "
    "{COMM_STYLE} {KILL_SIGNALS} {EXAMPLES_BLOCK} {EXAMPLES_DIR} "
    "{MEETING_DURATION} {MEETING_FORMAT} {SCHEDULING_LINK} "
    "{TIME_1} {TIME_2} {ROUND_FIT_REASONING} {PARTNER_NAME} "
    "{FUND_NAME} {PARTNER_BIO}"
)


def _build(channel: str) -> str:
    from core.email.prompt import build_live_prompt
    return build_live_prompt(
        prompt_template=_TEMPLATE,
        company_cfg=_CFG,
        partner_name="Sam", fund_name="Acme",
        partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/nowhere",
        channel=channel,
    )


# ---------- channel_style_note helper ----------

def test_channel_style_note_email_returns_email_format() -> None:
    from core.email.prompt import channel_style_note
    note = channel_style_note("email")
    assert "cold email" in note
    assert "subject line" in note


def test_channel_style_note_linkedin_returns_linkedin_format() -> None:
    from core.email.prompt import channel_style_note
    note = channel_style_note("linkedin")
    assert "LinkedIn DM" in note
    assert "NO subject line" in note
    assert "3 sentences" in note


def test_channel_style_note_both_returns_email_with_followup() -> None:
    from core.email.prompt import channel_style_note
    note = channel_style_note("both")
    # Primary draft is email; LinkedIn parallel is a follow-up.
    assert "email" in note.lower()
    assert "follow-up" in note.lower() or "parallel" in note.lower()


def test_channel_style_note_unknown_falls_back_to_email() -> None:
    """Defensive default: a stale frontend sending 'sms' or
    similar shouldn't crash; treat as email."""
    from core.email.prompt import channel_style_note
    note = channel_style_note("sms")
    assert "cold email" in note
    note_empty = channel_style_note("")
    assert "cold email" in note_empty


def test_channel_style_note_handles_case_and_whitespace() -> None:
    from core.email.prompt import channel_style_note
    # Mixed case + leading space → normalized.
    assert channel_style_note("  LinkedIn  ") == channel_style_note("linkedin")


# ---------- build_live_prompt threading ----------

def test_build_live_prompt_injects_linkedin_format_when_channel_linkedin() -> None:
    out = _build("linkedin")
    assert "LinkedIn DM" in out
    assert "NO subject line" in out
    # The literal CHANNEL_STYLE_NOTE placeholder should be gone.
    assert "{CHANNEL_STYLE_NOTE}" not in out
    # CHANNEL placeholder substituted too.
    assert "Channel: linkedin" in out


def test_build_live_prompt_email_default_keeps_existing_format() -> None:
    out = _build("email")
    assert "cold email" in out
    assert "subject line" in out
    assert "Channel: email" in out


def test_build_live_prompt_both_uses_email_with_followup_hint() -> None:
    out = _build("both")
    assert "Channel: both" in out
    # 'both' renders an email-style note (parallel LinkedIn is follow-up).
    assert "email" in out.lower()


def test_build_live_prompt_unknown_channel_falls_back_to_email() -> None:
    out = _build("smoke-signal")
    # CHANNEL token reflects what was sent (no normalization on the
    # CHANNEL placeholder itself -- just the style note).
    assert "Channel: smoke-signal" in out
    # Style note defaults to email.
    assert "cold email" in out
