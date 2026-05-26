"""Tests for the typed CompanyConfig view."""
from __future__ import annotations

import pytest

from core.company_config import CompanyConfig


# ---------- empty / missing inputs ---------------------------------------

def test_empty_dict_produces_sensible_defaults() -> None:
    """A fresh workspace's company.yaml may be empty; CompanyConfig
    must not raise + every accessor must return a defaulted value."""
    cfg = CompanyConfig.from_dict({})
    assert cfg.company.name == ""
    assert cfg.company.problem == ""
    assert cfg.company.target_sectors == []
    assert cfg.company.target_check_min_usd is None
    assert cfg.raise_context.round == ""
    assert cfg.founder_voice.banned_phrases == []
    assert cfg.round_fit.disqualifiers == []
    assert cfg.mode == "dry_run"  # default when absent


def test_none_input_is_treated_as_empty() -> None:
    """Defensive: code paths that haven't yet loaded a workspace
    pass None; CompanyConfig handles that without a NoneType error."""
    cfg = CompanyConfig.from_dict(None)
    assert cfg.company.name == ""
    assert cfg.raise_context.amount == ""


# ---------- typed accessors -----------------------------------------------

def test_flat_company_fields_read_through() -> None:
    cfg = CompanyConfig.from_dict({
        "company": {
            "name": "Acme",
            "problem": "Manual reporting is slow.",
            "solution": "API for reporting.",
            "target_sectors": ["fintech", "compliance"],
            "target_check_min_usd": 250000,
            "founded_year": 2024,
        },
    })
    assert cfg.company.name == "Acme"
    assert cfg.company.problem == "Manual reporting is slow."
    assert cfg.company.solution == "API for reporting."
    assert cfg.company.target_sectors == ["fintech", "compliance"]
    assert cfg.company.target_check_min_usd == 250000
    assert cfg.company.founded_year == 2024


def test_wrong_type_falls_back_to_default() -> None:
    """If a YAML field is the wrong type (e.g. someone wrote
    `problem: 42` by mistake), the accessor returns the default
    instead of propagating the int. Same for lists -- a stray
    string where a list was expected reads as []."""
    cfg = CompanyConfig.from_dict({
        "company": {
            "problem": 42,                # not a string
            "target_sectors": "fintech",  # not a list
        },
    })
    assert cfg.company.problem == ""
    assert cfg.company.target_sectors == []


def test_legacy_nested_target_check_size_falls_back() -> None:
    """Workspaces edited via the CLI use the older nested shape
    target_check_size_usd: {min, max}. The view falls through to
    that when the flat fields are absent so the migration off the
    nested shape is silent + non-breaking."""
    cfg = CompanyConfig.from_dict({
        "company": {
            "target_check_size_usd": {"min": 100000, "max": 1500000},
        },
    })
    assert cfg.company.target_check_min_usd == 100000
    assert cfg.company.target_check_max_usd == 1500000


def test_flat_target_check_overrides_legacy_nested() -> None:
    """When BOTH the flat and nested shapes are present, the flat
    wins -- it's what the onboarding wizard writes, and the nested
    shape is back-compat scaffolding."""
    cfg = CompanyConfig.from_dict({
        "company": {
            "target_check_min_usd": 250000,
            "target_check_size_usd": {"min": 100000, "max": 1500000},
        },
    })
    assert cfg.company.target_check_min_usd == 250000


def test_traction_falls_back_to_current_traction_headline_metric() -> None:
    """Same back-compat shape for traction. CLI-edited workspaces
    use `current_traction: {headline_metric: ...}`; the wizard
    writes a flat `traction:` string."""
    cfg = CompanyConfig.from_dict({
        "company": {
            "current_traction": {
                "headline_metric": "$180K ARR",
                "secondary_metrics": ["NRR 128%"],
            },
        },
    })
    assert cfg.company.traction == "$180K ARR"


def test_scheduling_link_falls_back_to_meeting_ask() -> None:
    cfg = CompanyConfig.from_dict({
        "company": {
            "meeting_ask": {
                "preferred_scheduling_link": "https://cal.example/x",
            },
        },
    })
    assert cfg.company.scheduling_link == "https://cal.example/x"


def test_bool_is_not_coerced_to_int() -> None:
    """Python's bool is a subclass of int. A YAML value of `true`
    where an int field was expected should read as None rather than
    becoming 1 (which would silently fake a check-size of $1)."""
    cfg = CompanyConfig.from_dict({
        "company": {"target_check_min_usd": True},
    })
    assert cfg.company.target_check_min_usd is None


# ---------- raise_context / founder_voice / round_fit ---------------------

def test_raise_context_fields_read_through() -> None:
    cfg = CompanyConfig.from_dict({
        "raise_context": {
            "round": "Seed",
            "amount": "$3M",
            "instrument": "priced",
            "strongest_raise_proof": "128% NRR",
        },
    })
    assert cfg.raise_context.round == "Seed"
    assert cfg.raise_context.amount == "$3M"
    assert cfg.raise_context.instrument == "priced"
    assert cfg.raise_context.strongest_raise_proof == "128% NRR"


def test_founder_voice_banned_phrases_is_a_list() -> None:
    cfg = CompanyConfig.from_dict({
        "founder_voice": {
            "style": "direct, no buzzwords",
            "banned_phrases": ["would love", "synergy"],
        },
    })
    assert cfg.founder_voice.style == "direct, no buzzwords"
    assert cfg.founder_voice.banned_phrases == ["would love", "synergy"]


def test_round_fit_disqualifiers_is_a_list() -> None:
    cfg = CompanyConfig.from_dict({
        "round_fit": {
            "disqualifiers": [
                "growth-only investor",
                "not currently deploying",
            ],
        },
    })
    assert cfg.round_fit.disqualifiers == [
        "growth-only investor",
        "not currently deploying",
    ]


# ---------- raw dict access ----------------------------------------------

def test_raw_preserves_full_dict_for_pass_through() -> None:
    """During the migration, some code paths still take a dict.
    Exposing `.raw` lets the typed view be the canonical form +
    the dict be a fallback rather than maintaining two parallel
    arguments at every call site."""
    src = {"company": {"name": "Acme"}, "mode": "production"}
    cfg = CompanyConfig.from_dict(src)
    assert cfg.raw is src
    assert cfg.raw["mode"] == "production"
    assert cfg.company.raw["name"] == "Acme"


def test_mode_defaults_to_dry_run() -> None:
    """An absent or non-string `mode:` reads as 'dry_run' -- the
    same default core/config_loader.py applies before this layer
    is reached."""
    assert CompanyConfig.from_dict({}).mode == "dry_run"
    assert CompanyConfig.from_dict({"mode": "fixture"}).mode == "fixture"
    # Bogus type falls back to default.
    assert CompanyConfig.from_dict({"mode": 42}).mode == "dry_run"
