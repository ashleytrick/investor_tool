"""Review item #11: per-tenant opt-in for the shared
investors_global discovery pool.

Default: NOT opted in -- uploads stay private until the operator
flips the toggle. The operator-level `INVESTORS_GLOBAL_DISABLED`
env var stays as a kill switch on top.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "test_workspace"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


@pytest.fixture
def client(workspace: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


# ---------- /settings/discovery-opt-in ----------

def test_default_opt_in_is_false(client) -> None:
    res = client.get("/settings/discovery-opt-in", headers=_auth())
    assert res.status_code == 200
    assert res.json() == {"opted_in": False}


def test_opt_in_round_trips(client) -> None:
    res = client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"opted_in": True}
    # Round-trip via GET.
    res2 = client.get("/settings/discovery-opt-in", headers=_auth())
    assert res2.json() == {"opted_in": True}


def test_opt_out_persists(client) -> None:
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True},
        headers=_auth(),
    )
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": False},
        headers=_auth(),
    )
    assert client.get(
        "/settings/discovery-opt-in", headers=_auth(),
    ).json() == {"opted_in": False}


def test_opt_in_requires_auth(client) -> None:
    assert client.get("/settings/discovery-opt-in").status_code == 401
    assert client.post(
        "/settings/discovery-opt-in", json={"opted_in": True},
    ).status_code == 401


# ---------- pipeline/sources upload respects opt-in ----------

_CSV_PAYLOAD = (
    b"name,domain\n"
    b"Example Ventures,example.vc\n"
    b"Beta Capital,beta.fund\n"
)


def test_sources_upload_skips_global_sync_by_default(
    client, monkeypatch,
) -> None:
    """First upload from a brand-new tenant must NOT sync to the
    global pool (opt-in default = False)."""
    calls = []

    def fake_sync(content):
        calls.append(content)
        return len(calls)

    import web.api as api_mod
    monkeypatch.setattr(
        api_mod, "_sync_uploaded_csv_to_global_pool", fake_sync,
    )

    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("investors.csv", _CSV_PAYLOAD, "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert calls == [], "global sync ran without opt-in"
    body = res.json()
    # No "synced N row(s)" mention in stdout.
    assert "synced" not in body["stdout"].lower()


def test_sources_upload_syncs_when_opted_in(client, monkeypatch) -> None:
    calls = []

    def fake_sync(content):
        calls.append(content)
        return 5  # pretend 5 rows synced

    import web.api as api_mod
    monkeypatch.setattr(
        api_mod, "_sync_uploaded_csv_to_global_pool", fake_sync,
    )

    # Opt in.
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True},
        headers=_auth(),
    )

    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("investors.csv", _CSV_PAYLOAD, "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert len(calls) == 1
    body = res.json()
    assert "synced 5" in body["stdout"]


def test_sources_upload_respects_opt_out_after_opting_in(
    client, monkeypatch,
) -> None:
    """Operator can opt in, then opt out -- subsequent uploads
    stop syncing."""
    calls = []

    def fake_sync(content):
        calls.append(content)
        return 1

    import web.api as api_mod
    monkeypatch.setattr(
        api_mod, "_sync_uploaded_csv_to_global_pool", fake_sync,
    )

    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": True}, headers=_auth(),
    )
    client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("a.csv", _CSV_PAYLOAD, "text/csv")},
    )
    assert len(calls) == 1

    # Opt out, upload again -> no new sync call.
    client.post(
        "/settings/discovery-opt-in",
        json={"opted_in": False}, headers=_auth(),
    )
    client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("b.csv", _CSV_PAYLOAD, "text/csv")},
    )
    assert len(calls) == 1, "global sync ran after opt-out"
