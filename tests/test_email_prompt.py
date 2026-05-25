"""Unit tests for core/email/prompt.py (Refactor item 14)."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.email.prompt import (
    build_live_prompt,
    meeting_slot,
    read_example_files,
)


# ----- meeting_slot -----


def test_meeting_slot_returns_configured_value() -> None:
    cfg = {"meeting_ask": {"preferred_time_slots": ["Tue 10am ET", "Wed 2pm ET"]}}
    assert meeting_slot(cfg, 0) == "Tue 10am ET"
    assert meeting_slot(cfg, 1) == "Wed 2pm ET"


def test_meeting_slot_falls_back_to_sentinel_when_index_missing() -> None:
    cfg = {"meeting_ask": {"preferred_time_slots": ["Tue 10am ET"]}}
    # idx=1 out of range -> sentinel.
    out = meeting_slot(cfg, 1)
    assert out == "(no time slot configured)"


def test_meeting_slot_handles_missing_meeting_ask_block() -> None:
    assert meeting_slot({}, 0) == "(no time slot configured)"


def test_meeting_slot_sentinel_is_not_a_curly_placeholder() -> None:
    """Regression guard: the fallback string MUST NOT look like
    `{TIME_1}` because check_hard_gates would then reject the body
    even though no real placeholder leaked through."""
    out = meeting_slot({}, 0)
    assert "{" not in out
    assert "}" not in out


# ----- read_example_files -----


def test_read_example_files_returns_sentinel_when_dir_absent(tmp_path: Path):
    out = read_example_files(tmp_path / "nope")
    assert out == "(no example files available)"


def test_read_example_files_returns_sentinel_when_dir_empty(tmp_path: Path):
    (tmp_path / "examples").mkdir()
    out = read_example_files(tmp_path / "examples")
    assert out == "(no example files available)"


def test_read_example_files_concatenates_with_headers(tmp_path: Path):
    d = tmp_path / "examples"
    d.mkdir()
    (d / "signal_led.md").write_text("First file body", encoding="utf-8")
    (d / "portfolio_led.md").write_text("Second file body", encoding="utf-8")
    out = read_example_files(d)
    # Alphabetical order: portfolio_led before signal_led.
    assert out.startswith("--- portfolio_led ---")
    assert "Second file body" in out
    assert "--- signal_led ---" in out
    assert "First file body" in out


def test_read_example_files_skips_blank_files(tmp_path: Path):
    d = tmp_path / "examples"
    d.mkdir()
    (d / "blank.md").write_text("   \n", encoding="utf-8")
    (d / "real.md").write_text("real body", encoding="utf-8")
    out = read_example_files(d)
    assert "blank" not in out
    assert "real body" in out


# ----- build_live_prompt -----


def _minimal_company() -> dict:
    return {
        "company": {
            "name": "Tendril",
            "founder_name": "Dana Cole",
            "description": "Regulatory reporting API",
            "current_traction": {
                "headline_metric": "$180K ARR",
                "secondary_metrics": ["128% NRR", "4 design partners"],
            },
            "meeting_ask": {
                "duration_minutes": 30,
                "format": "video call",
                "preferred_scheduling_link": "https://cal.example/dana",
                "preferred_time_slots": ["Tue 10am ET", "Wed 2pm ET"],
            },
        },
        "raise_context": {
            "round": "Seed",
            "amount": "$3M",
            "status": "fundraising",
            "timing": "first close 8 weeks",
            "why_this_round_is_fundable_now": "state mandates land Q3",
            "what_changes_after_this_round": "GTM hires",
            "round_hook": {
                "strongest_reason_to_meet_now": "buyers forced",
                "investor_consequence_of_waiting": "round closes",
                "round_momentum_proof": "two co-investors signed",
            },
            "strongest_raise_proof": "$180K ARR closed in 4 months",
            "notable_existing_investors_or_non_dilutive": "",
        },
        "founder_voice": {
            "style": "direct, lowercase",
            "banned_phrases": ["disrupt", "synergy"],
        },
    }


def test_build_live_prompt_fills_every_substituted_placeholder() -> None:
    template = (
        "Hi {PARTNER_NAME} at {FUND_NAME}. "
        "We are {COMPANY_NAME} ({COMPANY_DESCRIPTION}). "
        "Founder: {FOUNDER_NAME}. Raising {RAISE_AMOUNT} {ROUND}. "
        "Composite: {COMPOSITE_SCORE}. "
        "Times: {TIME_1} / {TIME_2}. "
        "Link: {SCHEDULING_LINK}."
    )
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=_minimal_company(),
        partner_name="Priya",
        fund_name="Northbeam",
        partner_bio="bio text",
        composite_score=7.85,
        round_fit_score=8.0,
        round_fit_reasoning="r",
        lead_likelihood_score=6.0,
        axes_summary="a1 (8.0), a2 (6.0)",
        fund_kill_signals=None,
        signals_for_partner=[
            {"quote": "wedge framing", "source_url": "https://a",
             "date": "2026-04-01"},
        ],
        deals_for_partner=[],
        examples_dir="/tmp/does-not-exist",
    )
    assert "Hi Priya at Northbeam" in out
    assert "Tendril" in out
    assert "$3M Seed" in out
    assert "7.85" in out  # composite formatted with 2 decimals
    assert "Tue 10am ET" in out
    assert "Wed 2pm ET" in out
    assert "https://cal.example/dana" in out


def test_build_live_prompt_score_none_leaves_blank_not_literal_None() -> None:
    """Finding 5: a None composite/round_fit/lead_likelihood should
    substitute to an empty string, not 'None'. Otherwise the LLM sees
    the word None and may include it verbatim."""
    template = "[{COMPOSITE_SCORE}][{ROUND_FIT_SCORE}][{LEAD_LIKELIHOOD_SCORE}]"
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=_minimal_company(),
        partner_name=None, fund_name=None, partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/tmp/does-not-exist",
    )
    assert out == "[][][]"


def test_build_live_prompt_missing_time_slot_uses_sentinel() -> None:
    """When preferred_time_slots is empty, {TIME_1} / {TIME_2} get the
    sentinel string rather than leaking literal `{TIME_1}` (which
    Stage 7's check_hard_gates would reject)."""
    company = _minimal_company()
    company["company"]["meeting_ask"]["preferred_time_slots"] = []
    template = "Times: {TIME_1} or {TIME_2}"
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=company,
        partner_name=None, fund_name=None, partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/tmp/does-not-exist",
    )
    assert "{TIME_1}" not in out
    assert "{TIME_2}" not in out
    assert "(no time slot configured)" in out


def test_build_live_prompt_top_signals_capped_at_three() -> None:
    """The {TOP_SIGNALS} placeholder should never carry more than 3
    rows -- otherwise the live prompt balloons + costs more tokens."""
    template = "[{TOP_SIGNALS}]"
    sigs = [
        {"quote": f"q{i}", "source_url": f"https://a{i}",
         "date": "2026-04-01"}
        for i in range(7)
    ]
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=_minimal_company(),
        partner_name=None, fund_name=None, partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=sigs, deals_for_partner=[],
        examples_dir="/tmp/does-not-exist",
    )
    # Three quotes embedded; q3+ should NOT appear.
    assert "q0" in out and "q1" in out and "q2" in out
    assert "q3" not in out
    assert "q6" not in out


def test_build_live_prompt_founder_voice_banned_phrases_joined() -> None:
    template = "[{FOUNDER_BANNED_PHRASES}]"
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=_minimal_company(),
        partner_name=None, fund_name=None, partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/tmp/does-not-exist",
    )
    assert "disrupt" in out
    assert "synergy" in out


def test_build_live_prompt_examples_block_substitutes_actual_content(
    tmp_path: Path,
) -> None:
    d = tmp_path / "examples"
    d.mkdir()
    (d / "signal_led.md").write_text("anchor body", encoding="utf-8")
    template = "Examples:\n{EXAMPLES_BLOCK}"
    out = build_live_prompt(
        prompt_template=template,
        company_cfg=_minimal_company(),
        partner_name=None, fund_name=None, partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir=d,
    )
    assert "--- signal_led ---" in out
    assert "anchor body" in out
