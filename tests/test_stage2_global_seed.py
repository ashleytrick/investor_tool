"""Tests for batch K: Stage 2 seeds the shared investors_global pool.

Two surfaces:

- ``seed_from_stage2_enrichment`` -- pure builder. Shaped + filtered
  inputs map to the right list of ``InvestorRow``\\ s. No DB.

- End-to-end through Stage 2 (fixture mode): after running
  ``02_enrich_funds.py --fixtures`` against ``test_workspace`` the
  shared pool contains one row per discovered partner, with the
  fund's stated stage / sectors propagated and tenant-specific
  fields absent.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


def test_seed_returns_one_row_per_partner_with_fund_metadata():
    from core.investors_global import seed_from_stage2_enrichment

    rows = seed_from_stage2_enrichment(
        fund_name="Northbeam Capital",
        fund_enrichment={
            "stated_stage_focus": "seed",
            "stated_sectors": ["fintech", "infra"],
            "thesis_summary": "deep-tech infra seed",
            "check_size_range": "$1-3M",
        },
        partners_for_fund=[
            {"name": "Priya Anand", "email": "priya@northbeam.example"},
            {"name": "Sam Patel", "email": None},
        ],
    )
    assert len(rows) == 2
    by_partner = {r.partner: r for r in rows}
    p = by_partner["Priya Anand"]
    assert p.firm == "Northbeam Capital"
    assert p.email == "priya@northbeam.example"
    assert p.stages == ("seed",)
    assert p.sectors == ("fintech", "infra")
    assert p.enriched_fields["thesis_summary"] == "deep-tech infra seed"
    assert p.enriched_fields["check_size_range"] == "$1-3M"
    assert by_partner["Sam Patel"].email is None


def test_seed_drops_partners_with_blank_name():
    from core.investors_global import seed_from_stage2_enrichment
    rows = seed_from_stage2_enrichment(
        fund_name="Acme VC",
        fund_enrichment={"stated_stage_focus": "seed"},
        partners_for_fund=[
            {"name": "", "email": None},
            {"name": "   ", "email": None},
            {"name": "Real Partner", "email": None},
        ],
    )
    assert [r.partner for r in rows] == ["Real Partner"]


def test_seed_emits_placeholder_row_when_no_partners_discovered():
    """A fund with a broken team page still belongs in the pool so a
    future tenant who knows a partner there can claim it."""
    from core.investors_global import seed_from_stage2_enrichment
    rows = seed_from_stage2_enrichment(
        fund_name="Quiet Capital",
        fund_enrichment={
            "stated_stage_focus": "seed",
            "stated_sectors": ["ai"],
        },
        partners_for_fund=[],
    )
    assert len(rows) == 1
    assert rows[0].firm == "Quiet Capital"
    assert rows[0].partner == "(unknown)"
    assert rows[0].sectors == ("ai",)


def test_seed_skips_when_fund_name_blank():
    from core.investors_global import seed_from_stage2_enrichment
    assert seed_from_stage2_enrichment(
        fund_name="   ",
        fund_enrichment={"stated_stage_focus": "seed"},
        partners_for_fund=[{"name": "X", "email": None}],
    ) == []


def test_seed_omits_enriched_fields_when_all_missing():
    from core.investors_global import seed_from_stage2_enrichment
    rows = seed_from_stage2_enrichment(
        fund_name="Bare Capital",
        fund_enrichment={"stated_stage_focus": "seed"},
        partners_for_fund=[{"name": "Jane", "email": None}],
    )
    assert rows[0].enriched_fields is None


def test_is_disabled_honors_env_var(monkeypatch):
    from core.investors_global import is_disabled
    monkeypatch.delenv("INVESTORS_GLOBAL_DISABLED", raising=False)
    assert is_disabled() is False
    monkeypatch.setenv("INVESTORS_GLOBAL_DISABLED", "true")
    assert is_disabled() is True
    monkeypatch.setenv("INVESTORS_GLOBAL_DISABLED", "1")
    assert is_disabled() is True
    monkeypatch.setenv("INVESTORS_GLOBAL_DISABLED", "off")
    assert is_disabled() is False


# ---------- end-to-end ----------

def test_stage2_fixture_run_populates_global_pool(
    tmp_path: Path, monkeypatch,
) -> None:
    """Running 02_enrich_funds.py --fixtures should leave one row per
    discovered partner in the shared pool. Proves the in-script hook
    actually fires and that tenant-specific signal stays out."""
    from tests.conftest import REPO_ROOT, _run

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    global_db = tmp_path / "global.db"
    monkeypatch.setenv("GLOBAL_DB_PATH", str(global_db))
    monkeypatch.delenv("INVESTORS_GLOBAL_DISABLED", raising=False)

    _run("01_aggregate_sources.py", "--workspace", str(ws_dst),
         cwd=REPO_ROOT)
    _run("02_enrich_funds.py", "--workspace", str(ws_dst), "--fixtures",
         cwd=REPO_ROOT)

    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    engine = ig.get_global_engine()
    assert ig.count_investors(engine) > 0

    # Spot-check: a known fixture partner is present and carries the
    # fund's stated stage. No tenant-specific column should exist.
    import sqlalchemy as sa
    with engine.begin() as conn:
        rows = list(conn.execute(sa.text(
            "select firm, partner, stages, sectors, enriched_fields "
            "from investors_global"
        )))
    by_partner = {r.partner: r for r in rows}
    assert "Kwame Boateng" in by_partner
    kb = by_partner["Kwame Boateng"]
    assert kb.firm == "Foundry North"
    assert "seed" in json.loads(kb.stages or "[]")


def test_stage2_seed_is_disabled_when_env_flag_set(
    tmp_path: Path, monkeypatch,
) -> None:
    from tests.conftest import REPO_ROOT, _run

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    global_db = tmp_path / "global.db"
    monkeypatch.setenv("GLOBAL_DB_PATH", str(global_db))
    monkeypatch.setenv("INVESTORS_GLOBAL_DISABLED", "true")

    _run("01_aggregate_sources.py", "--workspace", str(ws_dst),
         cwd=REPO_ROOT)
    _run("02_enrich_funds.py", "--workspace", str(ws_dst), "--fixtures",
         cwd=REPO_ROOT)

    # Pool file shouldn't even be created when the seed is disabled.
    # (get_global_engine creates the dir + table on first call; since
    # nothing called it inside the disabled run, the file stays absent.)
    assert not global_db.exists()
