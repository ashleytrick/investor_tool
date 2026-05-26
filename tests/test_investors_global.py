"""Tests for Phase 3 -- shared investors_global discovery pool.

Two surfaces:

- `core.investors_global` -- upsert + dedup logic. Pure unit tests
  against an in-memory engine.

- `/pipeline/sources` integration -- the upload endpoint now also
  seeds the discovery pool. Verified by reading the pool after a
  CSV upload.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------- helpers ----------

def _engine(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "GLOBAL_DB_PATH", str(tmp_path / "global.db"),
    )
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    return ig.get_global_engine(), ig


# ---------- table + upsert ----------

def test_upsert_inserts_first_row(tmp_path: Path, monkeypatch) -> None:
    engine, ig = _engine(tmp_path, monkeypatch)
    rid = ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam Capital",
        partner="Priya Anand",
        email="priya@northbeam.example",
        stages=("seed", "series-a"),
        sectors=("fintech",),
    ))
    assert rid > 0
    assert ig.count_investors(engine) == 1


def test_upsert_dedupes_on_email_case_insensitive(
    tmp_path: Path, monkeypatch,
) -> None:
    """Same email + different casing/whitespace = same row."""
    engine, ig = _engine(tmp_path, monkeypatch)
    a = ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam", partner="Priya",
        email="priya@northbeam.example",
    ))
    b = ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam Capital",  # different firm casing
        partner="Priya A.",         # different partner spelling
        email="  Priya@NORTHBEAM.example  ",
    ))
    assert a == b, "same normalized email must collapse to same row"
    assert ig.count_investors(engine) == 1


def test_upsert_dedupes_on_firm_partner_when_no_email(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without an email, (firm, partner) is the dedup key --
    case-insensitive comparison so 'NORTHBEAM' / 'northbeam' match."""
    engine, ig = _engine(tmp_path, monkeypatch)
    a = ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam", partner="Priya",
    ))
    b = ig.upsert_investor(engine, ig.InvestorRow(
        firm="northbeam", partner="PRIYA",
    ))
    assert a == b
    assert ig.count_investors(engine) == 1


def test_upsert_does_not_dedupe_across_different_partners(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two partners at the same firm without emails are two
    separate investors -- the discovery query treats partners as
    the addressable unit, not firms."""
    engine, ig = _engine(tmp_path, monkeypatch)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam", partner="Priya",
    ))
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="Northbeam", partner="Marcus",
    ))
    assert ig.count_investors(engine) == 2


def test_upsert_unions_arrays_on_update(
    tmp_path: Path, monkeypatch,
) -> None:
    """Second upsert adds NEW stages/sectors/geographies without
    dropping the existing ones."""
    engine, ig = _engine(tmp_path, monkeypatch)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
        sectors=("fintech",), stages=("seed",),
    ))
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
        sectors=("compliance",), stages=("series-a",),
        geographies=("US",),
    ))
    # Read back via SQLAlchemy.
    from sqlalchemy import select
    from core.investors_global import investors_global
    with engine.begin() as conn:
        row = conn.execute(select(investors_global).limit(1)).first()
    assert json.loads(row.sectors) == ["compliance", "fintech"]
    assert json.loads(row.stages) == ["seed", "series-a"]
    assert json.loads(row.geographies) == ["US"]


def test_upsert_merges_enriched_fields_with_latest_wins(
    tmp_path: Path, monkeypatch,
) -> None:
    """Enriched-fields dict updates same keys with the newest
    value but preserves keys only the older payload had."""
    engine, ig = _engine(tmp_path, monkeypatch)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
        enriched_fields={"check_size_range": "$500K-2M", "thesis": "B2B"},
    ))
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
        enriched_fields={"check_size_range": "$1M-3M", "stage_focus": "Seed"},
    ))
    from sqlalchemy import select
    from core.investors_global import investors_global
    with engine.begin() as conn:
        row = conn.execute(select(investors_global).limit(1)).first()
    merged = json.loads(row.enriched_fields)
    assert merged["check_size_range"] == "$1M-3M"   # overwritten
    assert merged["thesis"] == "B2B"                 # preserved
    assert merged["stage_focus"] == "Seed"           # new


def test_upsert_learns_email_when_first_upload_lacked_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """Initial firm+partner upload has no email; later upsert
    carries one -> existing row gets the email."""
    engine, ig = _engine(tmp_path, monkeypatch)
    rid = ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P",
    ))
    again = ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
    ))
    assert rid == again
    from sqlalchemy import select
    from core.investors_global import investors_global
    with engine.begin() as conn:
        row = conn.execute(select(investors_global).limit(1)).first()
    assert row.email == "p@x.example"


def test_upsert_does_not_overwrite_a_different_email(
    tmp_path: Path, monkeypatch,
) -> None:
    """If the existing row already has a (different) email, a new
    upsert with a different email is treated as a DIFFERENT
    investor -- two rows, not one."""
    engine, ig = _engine(tmp_path, monkeypatch)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
    ))
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p2@x.example",
    ))
    # Two distinct emails -> two distinct investors. Same firm +
    # partner string doesn't collapse them because the email key
    # wins.
    assert ig.count_investors(engine) == 2


def test_first_seen_preserved_last_enriched_bumped(
    tmp_path: Path, monkeypatch,
) -> None:
    """Updates do not move first_seen_at backward and do bump
    last_enriched_at forward."""
    engine, ig = _engine(tmp_path, monkeypatch)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
    ))
    from sqlalchemy import select
    from core.investors_global import investors_global
    with engine.begin() as conn:
        before = conn.execute(select(investors_global).limit(1)).first()
    # Force a measurable time gap.
    import time
    time.sleep(0.05)
    ig.upsert_investor(engine, ig.InvestorRow(
        firm="X", partner="P", email="p@x.example",
        sectors=("fintech",),
    ))
    with engine.begin() as conn:
        after = conn.execute(select(investors_global).limit(1)).first()
    assert after.first_seen_at == before.first_seen_at
    assert after.last_enriched_at > before.last_enriched_at


# ---------- /pipeline/sources integration ----------

def _csv_with_two_rows() -> bytes:
    return (
        b"Investor name,Website,Partner,Partner email\n"
        b"Northbeam Capital,https://northbeam.example,Priya Anand,"
        b"priya@northbeam.example\n"
        b"Tidewater Ventures,https://tidewater.example,Dana Cole,\n"
    )


def test_pipeline_sources_seeds_global_pool(
    tmp_path: Path, monkeypatch, workspace: Path,
) -> None:
    """Upload via the existing endpoint also seeds investors_global.
    Uses the existing FastAPI test client + isolated GLOBAL_DB_PATH
    so the pool is per-test."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv(
        "GLOBAL_DB_PATH", str(tmp_path / "global.db"),
    )
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    import web.api as api_mod
    importlib.reload(api_mod)

    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    # Review item #11: opt in first -- default is now off.
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True},
        headers={"Authorization": "Bearer test-api-key"},
    )
    res = client.post(
        "/pipeline/sources",
        headers={"Authorization": "Bearer test-api-key"},
        files={"file": (
            "openvc.csv", _csv_with_two_rows(), "text/csv",
        )},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "synced 2 row(s) into the shared discovery pool" in body["stdout"]

    # Pool now has both investors.
    engine = ig.get_global_engine()
    assert ig.count_investors(engine) == 2


def test_pipeline_sources_skips_global_pool_when_disabled(
    tmp_path: Path, monkeypatch, workspace: Path,
) -> None:
    """`INVESTORS_GLOBAL_DISABLED=true` short-circuits the dual-
    write -- the tenant upload still succeeds, the pool stays
    empty. Lets operators disable the discovery pool without
    breaking the upload path."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv(
        "GLOBAL_DB_PATH", str(tmp_path / "global.db"),
    )
    monkeypatch.setenv("INVESTORS_GLOBAL_DISABLED", "true")
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    import web.api as api_mod
    importlib.reload(api_mod)

    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    res = client.post(
        "/pipeline/sources",
        headers={"Authorization": "Bearer test-api-key"},
        files={"file": ("x.csv", _csv_with_two_rows(), "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert "synced" not in res.json()["stdout"].lower()
    # Pool is empty.
    engine = ig.get_global_engine()
    assert ig.count_investors(engine) == 0


def test_pipeline_sources_tenant_upload_succeeds_even_if_pool_fails(
    tmp_path: Path, monkeypatch, workspace: Path,
) -> None:
    """Defense-in-depth: an exception during the global-pool sync
    must not 5xx the tenant upload. The CSV still lands, the
    response carries a warning in stdout."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv(
        "GLOBAL_DB_PATH", str(tmp_path / "global.db"),
    )
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    import web.api as api_mod
    importlib.reload(api_mod)

    # Sabotage the sync function for this test.
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated pool outage")
    monkeypatch.setattr(
        api_mod, "_sync_uploaded_csv_to_global_pool", boom,
    )

    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    # Review item #11: opt in so the sync path actually runs and
    # we can observe its sabotage being caught.
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True},
        headers={"Authorization": "Bearer test-api-key"},
    )
    res = client.post(
        "/pipeline/sources",
        headers={"Authorization": "Bearer test-api-key"},
        files={"file": (
            "investors.csv", _csv_with_two_rows(), "text/csv",
        )},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert "global-pool sync failed" in body["stdout"]
    assert "simulated pool outage" in body["stdout"]
    # Tenant CSV is on disk.
    csv_path = workspace / "data" / "raw" / "investors.csv"
    assert csv_path.exists()
