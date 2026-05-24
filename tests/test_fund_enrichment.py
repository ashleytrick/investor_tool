"""Unit tests for core/fund_enrichment.py (Refactor item 7 / 11)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.fund_enrichment import (
    build_fund_update_values,
    compute_vanished_partners,
    partner_upsert_values,
)


_NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def _enrichment(**over) -> SimpleNamespace:
    """Default enrichment with every field filled. Tests override
    individual fields to exercise the preserve-on-empty branches."""
    base = dict(
        thesis_summary="b2b infra plays at seed",
        stated_stage_focus="seed",
        check_size_range="$250k-$1M",
        explicit_kill_signals=["pre-seed only", "never leads"],
        source_urls_used=[
            "https://example.com/", "https://example.com/portfolio",
        ],
        current_partners=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


# ----- build_fund_update_values -----


def test_always_includes_last_updated_and_source_urls() -> None:
    out = build_fund_update_values(_enrichment(), _NOW)
    assert out["last_updated"] == _NOW
    assert out["source_urls"] == (
        "https://example.com/; https://example.com/portfolio"
    )


def test_includes_thesis_when_llm_filled_it() -> None:
    out = build_fund_update_values(_enrichment(), _NOW)
    assert out["stated_thesis"] == "b2b infra plays at seed"


def test_preserves_on_empty_thesis() -> None:
    """The whole point of preserve-on-empty: when the LLM didn't fill
    thesis_summary this run, the column key must NOT appear in the
    update dict so the DB keeps whatever was there before."""
    out = build_fund_update_values(
        _enrichment(thesis_summary=None), _NOW,
    )
    assert "stated_thesis" not in out


def test_preserves_on_empty_string_too() -> None:
    """Defensive: an empty string from the LLM should also preserve
    (truthiness check, not None check)."""
    out = build_fund_update_values(
        _enrichment(thesis_summary=""), _NOW,
    )
    assert "stated_thesis" not in out


def test_kill_signals_joined_with_semicolons() -> None:
    out = build_fund_update_values(_enrichment(), _NOW)
    assert out["kill_signals"] == "pre-seed only; never leads"


def test_empty_kill_signals_list_preserves() -> None:
    out = build_fund_update_values(
        _enrichment(explicit_kill_signals=[]), _NOW,
    )
    assert "kill_signals" not in out


def test_all_fields_missing_only_metadata_lands() -> None:
    """A degenerate enrichment with no extracted facts should only
    bump last_updated + source_urls; every business column preserves."""
    out = build_fund_update_values(
        _enrichment(
            thesis_summary=None,
            stated_stage_focus=None,
            check_size_range=None,
            explicit_kill_signals=[],
        ),
        _NOW,
    )
    assert set(out.keys()) == {"last_updated", "source_urls"}


# ----- partner_upsert_values -----


def test_partner_upsert_values_shape_and_status() -> None:
    p = SimpleNamespace(name="Priya Anand", title="Partner",
                        bio_snippet="bio text")
    row = partner_upsert_values(
        fund_id="f1", fund_domain="northbeam.example",
        partner=p, now=_NOW,
    )
    assert row["fund_id"] == "f1"
    assert row["name"] == "Priya Anand"
    assert row["title"] == "Partner"
    assert row["bio"] == "bio text"
    # Team-page presence = likely_current per the brief's ladder.
    assert row["employment_status"] == "likely_current"
    assert row["last_updated"] == _NOW
    # partner_id is the deterministic slug from core.ids.partner_id_for.
    assert row["partner_id"]
    assert row["partner_id"].startswith("northbeam.example")


def test_partner_upsert_values_stable_id_across_runs() -> None:
    """Same fund+name -> same partner_id; this is the property Stage 2
    relies on for upsert idempotence."""
    p = SimpleNamespace(name="Priya Anand", title=None, bio_snippet=None)
    a = partner_upsert_values(
        fund_id="f1", fund_domain="northbeam.example",
        partner=p, now=_NOW,
    )
    b = partner_upsert_values(
        fund_id="f1", fund_domain="northbeam.example",
        partner=p, now=_NOW,
    )
    assert a["partner_id"] == b["partner_id"]


# ----- compute_vanished_partners -----


def test_vanished_diff_is_prior_minus_discovered() -> None:
    prior = ["p_a", "p_b", "p_c"]
    discovered = ["p_b"]
    assert compute_vanished_partners(prior, discovered) == ["p_a", "p_c"]


def test_vanished_returns_empty_when_nothing_changed() -> None:
    assert compute_vanished_partners(["p_a"], ["p_a"]) == []


def test_empty_discovered_skips_demotion_to_avoid_mass_invalidation() -> None:
    """A team page that produced zero partners is more likely an LLM
    extraction miss than a true mass-departure; returning [] makes
    Stage 2 skip the demotion -- the safety property the inline code
    used to enforce via `if discovered_pids:`."""
    assert compute_vanished_partners(["p_a", "p_b"], []) == []


def test_vanished_returns_sorted_list_for_stable_audit() -> None:
    """Stage 2 logs the count + uses .in_(...) -- the order doesn't
    matter for correctness, but a stable sort makes test assertions +
    log messages reproducible."""
    out = compute_vanished_partners({"p_z", "p_a", "p_m"}, {"p_a"})
    assert out == ["p_m", "p_z"]


def test_extra_discovered_partner_is_not_vanished() -> None:
    """A partner that's NEW this run (in discovered but not prior)
    is upserted by the caller; it's not vanished, so it must not
    appear in the demotion list."""
    out = compute_vanished_partners(["p_a"], ["p_a", "p_b_new"])
    assert out == []
