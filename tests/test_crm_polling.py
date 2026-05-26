"""B6: tests for CRM fast polling (activity + pipeline).

`AttioCRMClient` is fully mocked via `client_factory`. We never
hit the network in tests; the CRM HTTP layer is exercised in
production where the operator's real api_key is on file.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _connect_attio(workspace_path: Path, plaintext_key: str = "k-12345") -> None:
    """Insert a crm_connections row with a real Fernet ciphertext
    so `_connected_providers` can decrypt it."""
    import datetime as _dt
    from core.crm_secrets import encrypt_api_key, key_suffix
    from core.db import crm_connections, get_engine
    eng = get_engine(f"sqlite:///{workspace_path}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(crm_connections.insert().values(
            provider="attio",
            encrypted_api_key=encrypt_api_key(plaintext_key),
            key_suffix=key_suffix(plaintext_key),
            connected_at=_dt.datetime.now(_dt.timezone.utc),
            last_sync_status="idle",
        ))


def _seed_partner(workspace_path: Path, email: str = "p@x.example") -> str:
    from core.db import get_engine, partners
    eng = get_engine(f"sqlite:///{workspace_path}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="Partner", email=email,
        ))
    return "p_x"


# ---------- activity polling ----------

def test_poll_activity_inserts_attio_event(workspace: Path) -> None:
    _connect_attio(workspace)
    _seed_partner(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_activity_for_workspace
    from core.db import get_engine, outreach_events
    from sqlalchemy import select

    def fake_factory(provider, key):
        c = MagicMock()
        c.list_activities_since.return_value = [{
            "external_id": "attio-task-1",
            "occurred_at": _dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "subject": "Followup needed",
            "body_snippet": "ping the partner about deck",
            "recipient_email": "p@x.example",
            "thread_id": None,
            "kind": "note",
        }]
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_activity_for_workspace(ws, client_factory=fake_factory)
    assert len(results) == 1
    assert results[0].provider == "attio"
    assert results[0].inserted == 1
    assert results[0].error is None

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.source == "attio",
            )
        ).first()
    assert row is not None
    assert row.external_id == "attio-task-1"
    # Recipient matched to local partner.
    assert row.partner_id == "p_x"


def test_poll_activity_is_idempotent(workspace: Path) -> None:
    _connect_attio(workspace)
    _seed_partner(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_activity_for_workspace
    activity = {
        "external_id": "task-2",
        "occurred_at": _dt.datetime(
            2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
        ),
        "subject": "x",
        "body_snippet": "x",
        "recipient_email": "p@x.example",
        "thread_id": None,
        "kind": "note",
    }

    def fake_factory(p, k):
        c = MagicMock()
        c.list_activities_since.return_value = [activity]
        return c

    ws = load_workspace(str(workspace))
    r1 = poll_crm_activity_for_workspace(ws, client_factory=fake_factory)
    r2 = poll_crm_activity_for_workspace(ws, client_factory=fake_factory)
    assert r1[0].inserted == 1
    assert r2[0].inserted == 0  # already on file


def test_poll_activity_returns_empty_when_no_crm_connected(
    workspace: Path,
) -> None:
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_activity_for_workspace
    ws = load_workspace(str(workspace))
    results = poll_crm_activity_for_workspace(ws)
    assert results == []


def test_poll_activity_captures_crm_errors(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import CRMPollError, poll_crm_activity_for_workspace

    def fake_factory(p, k):
        c = MagicMock()
        c.list_activities_since.side_effect = CRMPollError("attio_http_503")
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_activity_for_workspace(ws, client_factory=fake_factory)
    assert len(results) == 1
    assert results[0].inserted == 0
    assert "attio_http_503" in (results[0].error or "")


def test_poll_activity_stamps_sync_status_on_success(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_activity_for_workspace
    from core.db import crm_connections, get_engine
    from sqlalchemy import select

    def fake_factory(p, k):
        c = MagicMock()
        c.list_activities_since.return_value = []
        return c

    ws = load_workspace(str(workspace))
    poll_crm_activity_for_workspace(ws, client_factory=fake_factory)

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        row = conn.execute(
            select(crm_connections).where(
                crm_connections.c.provider == "attio"
            )
        ).first()
    assert row.last_sync_status == "ok"
    assert row.last_sync_at is not None
    assert row.last_sync_error is None


def test_poll_activity_stamps_sync_status_on_error(workspace: Path) -> None:
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import CRMPollError, poll_crm_activity_for_workspace
    from core.db import crm_connections, get_engine
    from sqlalchemy import select

    def fake_factory(p, k):
        c = MagicMock()
        c.list_activities_since.side_effect = CRMPollError("rate_limit")
        return c

    ws = load_workspace(str(workspace))
    poll_crm_activity_for_workspace(ws, client_factory=fake_factory)
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        row = conn.execute(
            select(crm_connections).where(
                crm_connections.c.provider == "attio"
            )
        ).first()
    assert row.last_sync_status == "error"
    assert "rate_limit" in (row.last_sync_error or "")


# ---------- pipeline polling ----------

def test_poll_pipeline_upserts_partner_stage(workspace: Path) -> None:
    _connect_attio(workspace)
    _seed_partner(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_pipeline_for_workspace
    from core.db import get_engine, partner_pipeline
    from sqlalchemy import select

    def fake_factory(p, k):
        c = MagicMock()
        c.list_pipeline_updates_since.return_value = [{
            "partner_email": "p@x.example",
            "stage": "in_discussions",
            "updated_at": _dt.datetime(
                2026, 5, 27, 10, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "notes": "Replied to follow-up",
        }]
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_pipeline_for_workspace(ws, client_factory=fake_factory)
    assert results[0].inserted == 1

    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        row = conn.execute(
            select(partner_pipeline).where(
                partner_pipeline.c.partner_id == "p_x"
            )
        ).first()
    assert row.stage == "in_discussions"
    assert row.updated_by == "crm:attio"


def test_poll_pipeline_skips_unknown_partner(workspace: Path) -> None:
    """An update for an email we don't have a partner row for is
    a no-op -- we don't fabricate partners from CRM data."""
    _connect_attio(workspace)
    from core.config_loader import load_workspace
    from core.crm_polling import poll_crm_pipeline_for_workspace

    def fake_factory(p, k):
        c = MagicMock()
        c.list_pipeline_updates_since.return_value = [{
            "partner_email": "stranger@x.example",
            "stage": "won",
            "updated_at": _dt.datetime.now(_dt.timezone.utc),
        }]
        return c

    ws = load_workspace(str(workspace))
    results = poll_crm_pipeline_for_workspace(ws, client_factory=fake_factory)
    assert results[0].inserted == 0


# ---------- FastAPI hook endpoints ----------

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


def test_hook_poll_crm_activity_requires_secret(client) -> None:
    res = client.post("/api/public/hooks/poll-crm-activity")
    assert res.status_code == 401


def test_hook_poll_crm_pipeline_requires_secret(client) -> None:
    res = client.post("/api/public/hooks/poll-crm-pipeline")
    assert res.status_code == 401


def test_hook_aggregates_per_tenant_results(
    client, workspace, monkeypatch,
) -> None:
    from core.crm_polling import PollResult as _PR

    def fake_activity_poll(ws, client_factory=None):
        return [_PR(
            workspace=str(ws.path), provider="attio", inserted=4,
        )]

    with patch(
        "core.crm_polling.poll_crm_activity_for_workspace",
        fake_activity_poll,
    ):
        res = client.post(
            "/api/public/hooks/poll-crm-activity",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["polled"] == 1
    assert body["total_inserted"] == 4
    assert body["results"][0]["provider"] == "attio"


def test_hook_pipeline_aggregates(client, workspace) -> None:
    from core.crm_polling import PollResult as _PR

    def fake_pipeline_poll(ws, client_factory=None):
        return [_PR(
            workspace=str(ws.path), provider="attio", inserted=2,
        )]

    with patch(
        "core.crm_polling.poll_crm_pipeline_for_workspace",
        fake_pipeline_poll,
    ):
        res = client.post(
            "/api/public/hooks/poll-crm-pipeline",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total_inserted"] == 2
