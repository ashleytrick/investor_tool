"""B7 (CRM investors + relationships 6h), B8 (CRM lists + deals
1h), B9 (one-shot bulk import on connect).

All four poll functions + the bulk-import callable share the same
client-factory injection seam as B6's poll-activity / poll-pipeline,
so tests inject fake CRM clients without touching the network.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", _gen_key())


def _connect_attio(workspace_path: Path) -> None:
    import datetime as _dt
    from core.crm_secrets import encrypt_api_key, key_suffix
    from core.db import crm_connections, get_engine
    eng = get_engine(f"sqlite:///{workspace_path}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(crm_connections.insert().values(
            provider="attio",
            encrypted_api_key=encrypt_api_key("k-12345"),
            key_suffix="2345",
            connected_at=_dt.datetime.now(_dt.timezone.utc),
            last_sync_status="idle",
        ))


# ---------- B7: investors ----------

def test_poll_investors_creates_funds_and_partners(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_investors_for_workspace
    from core.db import funds, get_engine, partners
    from sqlalchemy import select

    def fake_factory(p, k):
        c = MagicMock()
        c.list_investors.return_value = [
            {"firm": "Acme VC", "partner": "Jane Smith",
             "email": "jane@acme.example",
             "attio_person_id": "p-1", "attio_company_id": "c-1"},
            {"firm": "Beta Capital", "partner": "Bob Doe",
             "email": None,
             "attio_person_id": None, "attio_company_id": None},
        ]
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_investors_for_workspace(ws, client_factory=fake_factory)
    assert results[0].provider == "attio"
    assert results[0].inserted == 2

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        all_funds = list(conn.execute(select(funds)))
        all_partners = list(conn.execute(select(partners)))
    assert len(all_funds) == 2
    assert len(all_partners) == 2
    # Beta Capital -> unclaimed slug + provisional flag.
    beta_fund = next(f for f in all_funds if f.name == "Beta Capital")
    assert beta_fund.domain.endswith(".unclaimed")
    assert beta_fund.is_provisional is True


def test_poll_investors_idempotent_on_repeat(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_investors_for_workspace

    payload = [
        {"firm": "Acme", "partner": "Jane",
         "email": "j@acme.example",
         "attio_person_id": None, "attio_company_id": None},
    ]

    def fake_factory(p, k):
        c = MagicMock()
        c.list_investors.return_value = payload
        return c

    ws = load_workspace(str(workspace))
    r1 = poll_crm_investors_for_workspace(ws, client_factory=fake_factory)
    r2 = poll_crm_investors_for_workspace(ws, client_factory=fake_factory)
    assert r1[0].inserted == 1
    assert r2[0].inserted == 0  # already on file


def test_poll_investors_backfills_email_when_partner_id_is_stable(
    workspace: Path,
) -> None:
    """When both imports carry the same email (so partner_id is
    stable on (domain, name)), the second import is idempotent
    -- no new row -- and the email persists. Cross-domain
    backfill (no-email -> email) is a separate concern that
    requires a different reconciliation strategy."""
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_investors_for_workspace
    from core.db import get_engine, partners
    from sqlalchemy import select, func

    payload = [{"firm": "X", "partner": "P",
                "email": "p@x.example",
                "attio_person_id": None, "attio_company_id": None}]

    def factory(p, k):
        c = MagicMock(); c.list_investors.return_value = payload; return c

    ws = load_workspace(str(workspace))
    poll_crm_investors_for_workspace(ws, client_factory=factory)
    poll_crm_investors_for_workspace(ws, client_factory=factory)

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(partners)
        ).scalar()
        row = conn.execute(select(partners)).first()
    assert n == 1
    assert row.email == "p@x.example"


# ---------- B7: relationships ----------

def test_poll_relationships_inserts_events_for_known_partners(
    workspace: Path,
) -> None:
    _connect_attio(workspace)
    from core.db import get_engine, outreach_events, partners
    from sqlalchemy import select

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="P", email="p@x.example",
        ))

    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_relationships_for_workspace

    def fake_factory(p, k):
        c = MagicMock()
        c.list_relationships_since.return_value = [{
            "partner_email": "p@x.example",
            "rel_type": "intro_made",
            "occurred_at": _dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "notes": "Mutual connection introduced via Andy",
        }]
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_relationships_for_workspace(
        ws, client_factory=fake_factory,
    )
    assert results[0].inserted == 1
    with eng.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.source == "attio",
                outreach_events.c.event_type == "replied",
            )
        ).first()
    assert row.partner_id == "p_x"
    assert "intro_made" in (row.subject or "")


# ---------- B8: lists ----------

def test_poll_lists_replaces_provider_snapshot(workspace: Path) -> None:
    """Replace-based: lists removed in the CRM disappear from the
    local snapshot on the next pass."""
    _connect_attio(workspace)
    from core.db import crm_list_memberships, get_engine, partners
    from sqlalchemy import select

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="P", email="p@x.example",
        ))

    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_lists_for_workspace

    first = [
        {"list_name": "Warm intros", "partner_email": "p@x.example"},
        {"list_name": "Cold list", "partner_email": "p@x.example"},
    ]
    second = [
        {"list_name": "Warm intros", "partner_email": "p@x.example"},
    ]  # Cold list removed.

    def factory_first(p, k):
        c = MagicMock(); c.list_list_memberships.return_value = first; return c
    def factory_second(p, k):
        c = MagicMock(); c.list_list_memberships.return_value = second; return c

    ws = load_workspace(str(workspace))
    poll_crm_lists_for_workspace(ws, client_factory=factory_first)
    with eng.begin() as conn:
        names_before = [
            r.list_name for r in conn.execute(select(crm_list_memberships))
        ]
    assert sorted(names_before) == ["Cold list", "Warm intros"]

    poll_crm_lists_for_workspace(ws, client_factory=factory_second)
    with eng.begin() as conn:
        names_after = [
            r.list_name for r in conn.execute(select(crm_list_memberships))
        ]
    assert names_after == ["Warm intros"]


# ---------- B8: deals ----------

def test_poll_deals_upserts_and_is_idempotent(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.db import crm_deals, get_engine, partners
    from sqlalchemy import select

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="P", email="p@x.example",
        ))

    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_deals_for_workspace

    payload = [{
        "deal_id": "deal-123",
        "stage": "Term Sheet",
        "partner_email": "p@x.example",
        "amount": 250000.0,
        "updated_at": _dt.datetime(
            2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
        ),
    }]

    def factory(p, k):
        c = MagicMock(); c.list_deals_since.return_value = payload; return c

    ws = load_workspace(str(workspace))
    poll_crm_deals_for_workspace(ws, client_factory=factory)
    poll_crm_deals_for_workspace(ws, client_factory=factory)

    with eng.begin() as conn:
        rows = list(conn.execute(select(crm_deals)))
    assert len(rows) == 1
    assert rows[0].stage == "Term Sheet"
    assert rows[0].partner_id == "p_x"
    assert rows[0].amount == 250000.0


# ---------- B9: bulk import ----------

def test_bulk_import_is_alias_of_investors_poll(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import bulk_import_for_workspace
    from core.db import get_engine, partners
    from sqlalchemy import select, func

    def factory(p, k):
        c = MagicMock()
        c.list_investors.return_value = [
            {"firm": "A", "partner": "P1",
             "email": "p1@a.example",
             "attio_person_id": None, "attio_company_id": None},
            {"firm": "B", "partner": "P2",
             "email": "p2@b.example",
             "attio_person_id": None, "attio_company_id": None},
        ]
        return c

    ws = load_workspace(str(workspace))
    results = bulk_import_for_workspace(ws, client_factory=factory)
    assert results[0].inserted == 2

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(partners)
        ).scalar()
    assert n == 2


# ---------- FastAPI: all four hooks + bulk-import endpoint ----------

@pytest.fixture
def client(workspace, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("HOOK_SECRET", "s3")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


@pytest.mark.parametrize("path", [
    "/api/public/hooks/poll-crm-investors",
    "/api/public/hooks/poll-crm-relationships",
    "/api/public/hooks/poll-crm-lists",
    "/api/public/hooks/poll-crm-deals",
])
def test_each_new_hook_requires_secret(client, path) -> None:
    assert client.post(path).status_code == 401


def test_bulk_import_endpoint_returns_count(
    client, workspace, monkeypatch,
) -> None:
    _connect_attio(workspace)
    from core.crm_polling import PollResult as _PR

    def fake_bulk(ws, client_factory=None):
        return [_PR(workspace=str(ws.path), provider="attio", inserted=7)]

    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    monkeypatch.setattr(
        api_mod, "_ws_path", lambda: str(workspace),
    )
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    monkeypatch.setattr(
        "core.crm_polling.bulk_import_for_workspace", fake_bulk,
    )
    res = c.post(
        "/crm/bulk-import",
        json={"provider": "attio"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "attio"
    assert body["imported"] == 7


def test_bulk_import_rejects_unsupported_provider(client) -> None:
    res = client.post(
        "/crm/bulk-import",
        json={"provider": "myspace"},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_bulk_import_404_when_provider_not_connected(client) -> None:
    """Provider is supported but the tenant hasn't connected it ->
    404 so the frontend can prompt /crm/connect first."""
    res = client.post(
        "/crm/bulk-import",
        json={"provider": "attio"},
        headers=_auth(),
    )
    assert res.status_code == 404
