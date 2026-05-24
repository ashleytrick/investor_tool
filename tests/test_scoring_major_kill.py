"""Unit tests for core/scoring/major_kill.py (Refactor item 7 / 13)."""
from __future__ import annotations

from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.scoring.major_kill import MajorKill, aggregate_major_kill


def _rf(*, disqualifier=False, triggered=(), components=None) -> SimpleNamespace:
    return SimpleNamespace(
        disqualifier_present=disqualifier,
        triggered_disqualifiers=list(triggered),
        components=components if components is not None else {"active_fund": 1},
    )


def _fund(*, is_active=True, kill_signals=None) -> SimpleNamespace:
    return SimpleNamespace(is_active=is_active, kill_signals=kill_signals)


def _partner(*, employment_status="likely_current",
             do_not_contact=False, do_not_contact_reason=None
             ) -> SimpleNamespace:
    return SimpleNamespace(
        employment_status=employment_status,
        do_not_contact=do_not_contact,
        do_not_contact_reason=do_not_contact_reason,
    )


# ----- present=False paths -----


def test_clean_partner_not_killed() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(),
        partner=_partner(),
    )
    assert mk.present is False
    assert mk.summary == ""


# ----- present=True branches -----


def test_round_fit_disqualifier_flags_kill_and_lists_reasons() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(
            disqualifier=True,
            triggered=["wrong stage", "wrong check size"],
        ),
        fund=_fund(),
        partner=_partner(),
    )
    assert mk.present is True
    assert "wrong stage" in mk.summary
    assert "wrong check size" in mk.summary


def test_left_fund_status_flags_kill() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(),
        partner=_partner(employment_status="left_fund"),
    )
    assert mk.present is True


def test_inactive_fund_without_active_fund_component_flags_kill() -> None:
    """Defense-in-depth: even if round_fit didn't disqualify, an
    inactive fund whose active_fund component was 0 still kills."""
    mk = aggregate_major_kill(
        round_fit_result=_rf(components={"active_fund": 0}),
        fund=_fund(is_active=False),
        partner=_partner(),
    )
    assert mk.present is True


def test_inactive_fund_but_active_component_present_does_not_kill() -> None:
    """If round_fit somehow already accounted for the active_fund
    bonus, we trust it and don't double-kill."""
    mk = aggregate_major_kill(
        round_fit_result=_rf(components={"active_fund": 1}),
        fund=_fund(is_active=False),
        partner=_partner(),
    )
    assert mk.present is False


def test_fund_kill_signals_string_flags_kill_with_text_in_summary() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(kill_signals="pre-seed only; never leads"),
        partner=_partner(),
    )
    assert mk.present is True
    assert "fund kill_signals: pre-seed only" in mk.summary


def test_whitespace_only_kill_signals_does_not_count() -> None:
    """Defensive: kill_signals is operator-edited free text; a row
    that's literally just '   \\n  ' shouldn't trigger a kill."""
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(kill_signals="   \n  "),
        partner=_partner(),
    )
    assert mk.present is False


def test_do_not_contact_flags_kill_with_reason_in_summary() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(),
        partner=_partner(do_not_contact=True,
                         do_not_contact_reason="asked off-list"),
    )
    assert mk.present is True
    assert "do_not_contact: asked off-list" in mk.summary


def test_do_not_contact_without_reason_falls_back_to_dash() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(),
        partner=_partner(do_not_contact=True),
    )
    assert mk.present is True
    assert "do_not_contact: -" in mk.summary


def test_multiple_sources_combine_into_one_summary() -> None:
    mk = aggregate_major_kill(
        round_fit_result=_rf(
            disqualifier=True,
            triggered=["wrong stage"],
        ),
        fund=_fund(kill_signals="pre-seed only"),
        partner=_partner(do_not_contact=True,
                         do_not_contact_reason="asked off-list"),
    )
    assert mk.present is True
    # All three sources land in the summary, separated by "; ".
    assert "wrong stage" in mk.summary
    assert "fund kill_signals:" in mk.summary
    assert "do_not_contact:" in mk.summary


# ----- structural -----


def test_majorkill_namedtuple_fields() -> None:
    """Guard: any future addition should keep the (present, summary)
    field order so existing tuple-unpacking call sites don't silently
    swap roles."""
    assert MajorKill._fields == ("present", "summary")


def test_missing_do_not_contact_column_falls_back_safely() -> None:
    """Older partners rows pre-dating do_not_contact don't raise --
    getattr() defaults False so the aggregation tolerates the gap."""
    partner_no_dnc = SimpleNamespace(employment_status="likely_current")
    mk = aggregate_major_kill(
        round_fit_result=_rf(),
        fund=_fund(),
        partner=partner_no_dnc,
    )
    assert mk.present is False
