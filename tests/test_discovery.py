"""Tests for Phase 4 -- discovery endpoints.

Covers `core.discovery.find_matches` + `claim_investor` as units,
plus the `/discovery/matches` and `/discovery/claim` HTTP surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------- helpers ----------

def _seed_global(monkeypatch, tmp_path: Path, rows: list[dict]):
    """Set up the global pool with a synthetic set of rows. Returns
    the (reloaded) module + engine + list of inserted ids."""
    monkeypatch.setenv("GLOBAL_DB_PATH", str(tmp_path / "global.db"))
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    engine = ig.get_global_engine()
    ids = []
    for r in rows:
        rid = ig.upsert_investor(engine, ig.InvestorRow(**r))
        ids.append(rid)
    return ig, engine, ids


def _client(workspace: Path, monkeypatch, tmp_path: Path):
    """Build a FastAPI TestClient with the global pool isolated."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv("GLOBAL_DB_PATH", str(tmp_path / "global.db"))
    import importlib
    from core import investors_global as ig
    importlib.reload(ig)
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app), api_mod


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


# ---------- core.discovery.find_matches ----------

def test_find_matches_excludes_partners_tenant_already_has(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """The whole point of discovery is surfacing investors the
    tenant DOESN'T already have. Pre-seed both pool + tenant
    partners with the same (firm, partner) and confirm the
    match is filtered out."""
    from core.config_loader import load_workspace
    from core.db import funds, get_engine, partners
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "Northbeam", "partner": "Priya Anand",
         "sectors": ("fintech",)},
        {"firm": "Tidewater", "partner": "Dana Cole",
         "sectors": ("fintech",)},
    ])
    ws = load_workspace(str(workspace))
    eng = get_engine(ws.db_url)
    now = datetime.now(timezone.utc)
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="northbeam.example", name="Northbeam",
            domain="northbeam.example", last_updated=now,
        ))
        conn.execute(partners.insert().values(
            partner_id="northbeam.example_priya_anand",
            fund_id="northbeam.example", name="Priya Anand",
            last_updated=now,
        ))

    from core.discovery import find_matches
    from core.investors_global import get_global_engine
    matches = find_matches(eng, get_global_engine(), ws.company)
    firms = [m.firm for m in matches]
    assert "Northbeam" not in firms
    assert "Tidewater" in firms


def test_find_matches_ranks_by_sector_stage_geo_overlap(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """A pool with several investors at varying overlap with the
    fixture workspace's company.yaml (which targets fintech +
    compliance + US) sorts the best fit first."""
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "Perfect Fit", "partner": "A",
         "sectors": ("fintech", "compliance"),
         "geographies": ("United States",)},
        {"firm": "Some Overlap", "partner": "B",
         "sectors": ("fintech",)},
        {"firm": "No Overlap", "partner": "C",
         "sectors": ("consumer",)},
    ])
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.discovery import find_matches
    from core.investors_global import get_global_engine
    ws = load_workspace(str(workspace))
    matches = find_matches(
        get_engine(ws.db_url), get_global_engine(), ws.company,
    )
    firms_in_order = [m.firm for m in matches]
    # Perfect Fit > Some Overlap > No Overlap
    assert firms_in_order.index("Perfect Fit") < firms_in_order.index(
        "Some Overlap"
    )
    assert firms_in_order.index("Some Overlap") < firms_in_order.index(
        "No Overlap"
    )


def test_find_matches_attaches_fit_reasons(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """Each match's fit_reasons explains WHY it scored where it
    did. The frontend renders these next to the Claim button."""
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "F1", "partner": "P1",
         "sectors": ("fintech", "compliance"),
         "geographies": ("United States",)},
    ])
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.discovery import find_matches
    from core.investors_global import get_global_engine
    ws = load_workspace(str(workspace))
    matches = find_matches(
        get_engine(ws.db_url), get_global_engine(), ws.company,
    )
    assert len(matches) == 1
    reasons = matches[0].fit_reasons
    # Should mention sector overlap (compliance + fintech are in
    # test_workspace's target_sectors).
    assert any("sector overlap" in r for r in reasons), reasons


def test_find_matches_respects_limit(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    _seed_global(monkeypatch, tmp_path, [
        {"firm": f"Firm{i}", "partner": f"P{i}",
         "sectors": ("fintech",)}
        for i in range(20)
    ])
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.discovery import find_matches
    from core.investors_global import get_global_engine
    ws = load_workspace(str(workspace))
    matches = find_matches(
        get_engine(ws.db_url), get_global_engine(), ws.company,
        limit=5,
    )
    assert len(matches) == 5


# ---------- core.discovery.claim_investor ----------

def test_claim_creates_funds_and_partners_rows(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    from core.config_loader import load_workspace
    from core.db import funds, get_engine, partners
    from core.discovery import claim_investor
    from core.investors_global import get_global_engine

    _seed_global(monkeypatch, tmp_path, [
        {
            "firm": "Northbeam Capital",
            "partner": "Priya Anand",
            "email": "priya@northbeam.example",
            "sectors": ("fintech",),
            "enriched_fields": {"thesis": "B2B compliance"},
        },
    ])
    ws = load_workspace(str(workspace))
    eng = get_engine(ws.db_url)
    res = claim_investor(eng, get_global_engine(), 1)

    assert res.created_fund is True
    assert res.created_partner is True
    assert res.global_id == 1

    # Funds + partners now have the row.
    from sqlalchemy import select
    with eng.begin() as conn:
        fund = conn.execute(
            select(funds).where(funds.c.fund_id == res.fund_id)
        ).first()
        partner = conn.execute(
            select(partners).where(
                partners.c.partner_id == res.partner_id,
            )
        ).first()
    assert fund.name == "Northbeam Capital"
    assert partner.name == "Priya Anand"
    assert partner.claimed_from_global_id == 1
    # Thesis came through from enriched_fields.
    assert fund.stated_thesis == "B2B compliance"


def test_claim_is_idempotent(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """Re-claiming the same global_id is a no-op (same fund_id +
    partner_id, created flags now False) so the frontend can
    deep-link without checking + the operator can't accidentally
    duplicate."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.discovery import claim_investor
    from core.investors_global import get_global_engine
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "X", "partner": "P", "email": "p@x.example"},
    ])
    eng = get_engine(load_workspace(str(workspace)).db_url)
    first = claim_investor(eng, get_global_engine(), 1)
    second = claim_investor(eng, get_global_engine(), 1)
    assert first.fund_id == second.fund_id
    assert first.partner_id == second.partner_id
    assert second.created_fund is False
    assert second.created_partner is False


def test_claim_unknown_global_id_raises(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.discovery import ClaimError, claim_investor
    from core.investors_global import get_global_engine
    _seed_global(monkeypatch, tmp_path, [])
    eng = get_engine(load_workspace(str(workspace)).db_url)
    with pytest.raises(ClaimError):
        claim_investor(eng, get_global_engine(), 99999)


# ---------- HTTP surface ----------

def test_discovery_matches_endpoint_returns_ranked_list(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "A", "partner": "P", "sectors": ("fintech",)},
        {"firm": "B", "partner": "P", "sectors": ("consumer",)},
    ])
    client, _ = _client(workspace, monkeypatch, tmp_path)
    res = client.get("/discovery/matches", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    # Fit reasons surface in the response.
    assert all("fit_reasons" in m for m in body["matches"])
    # Fintech investor scores higher than consumer for the
    # test_workspace fixture.
    assert body["matches"][0]["firm"] == "A"


def test_discovery_claim_endpoint_round_trips(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "Northbeam", "partner": "Priya",
         "email": "priya@northbeam.example",
         "sectors": ("fintech",)},
    ])
    client, _ = _client(workspace, monkeypatch, tmp_path)
    res = client.post(
        "/discovery/claim",
        headers=_auth_headers(),
        json={"global_id": 1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["created_fund"] is True
    assert body["created_partner"] is True
    assert "Northbeam" in body["stdout"] or "claimed" in body["stdout"]

    # Same claim again -> created flags False, same ids.
    res2 = client.post(
        "/discovery/claim",
        headers=_auth_headers(),
        json={"global_id": 1},
    )
    assert res2.status_code == 200
    assert res2.json()["fund_id"] == body["fund_id"]
    assert res2.json()["created_fund"] is False


def test_discovery_claim_unknown_id_returns_404(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    _seed_global(monkeypatch, tmp_path, [])
    client, _ = _client(workspace, monkeypatch, tmp_path)
    res = client.post(
        "/discovery/claim",
        headers=_auth_headers(),
        json={"global_id": 99999},
    )
    assert res.status_code == 404


def test_discovery_endpoints_require_auth(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    _seed_global(monkeypatch, tmp_path, [])
    client, _ = _client(workspace, monkeypatch, tmp_path)
    assert client.get("/discovery/matches").status_code == 401
    assert client.post(
        "/discovery/claim", json={"global_id": 1},
    ).status_code == 401


def test_discovery_matches_filters_already_claimed(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """End-to-end: GET matches -> POST claim -> GET matches again
    no longer surfaces that investor."""
    _seed_global(monkeypatch, tmp_path, [
        {"firm": "F1", "partner": "P1", "email": "p@f1.example",
         "sectors": ("fintech",)},
        {"firm": "F2", "partner": "P2", "email": "p@f2.example",
         "sectors": ("fintech",)},
    ])
    client, _ = _client(workspace, monkeypatch, tmp_path)
    initial = client.get(
        "/discovery/matches", headers=_auth_headers(),
    ).json()
    assert initial["count"] == 2
    target = initial["matches"][0]["global_id"]
    client.post(
        "/discovery/claim", headers=_auth_headers(),
        json={"global_id": target},
    )
    after = client.get(
        "/discovery/matches", headers=_auth_headers(),
    ).json()
    assert after["count"] == 1
    assert all(m["global_id"] != target for m in after["matches"])
