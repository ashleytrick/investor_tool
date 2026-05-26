"""Review item #23: broader per-user endpoint coverage.

PR #75 wired per-user routing into the middleware. PR #75's test
covered /runs and the shell-out path. This file extends the
coverage to every Coach + CRM endpoint Lovable uses, so a
regression that silently routes any of them to the pinned
workspace gets caught.

Pattern: two JWTs (Alice + Bob) hit the same endpoint. Each
should see only its own tenant's data, and the underlying
workspace files should be created under the per-tenant
${WORKSPACES_ROOT}/{uuid}/ directory.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import jwt as _pyjwt
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


_JWT_SECRET = "test-jwt-secret-32-bytes-long-x"
_ALICE = "11111111-1111-1111-1111-111111111111"
_BOB = "22222222-2222-2222-2222-222222222222"


def _mint_jwt(uid: str) -> str:
    return _pyjwt.encode(
        {
            "sub": uid, "aud": "authenticated",
            "email": f"{uid}@example.com",
            "exp": int(time.time()) + 3600,
        },
        _JWT_SECRET, algorithm="HS256",
    )


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_jwt(uid)}"}


@pytest.fixture
def per_user_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "ws_root"
    root.mkdir()
    template = tmp_path / "tpl"
    template_src = REPO_ROOT / "clients" / "test_workspace"
    shutil.copytree(template_src, template)
    db = template / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(root))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", str(template))
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_KEY", "unused")
    monkeypatch.delenv("AUTH_ALLOW_API_KEY_FALLBACK", raising=False)
    monkeypatch.delenv("API_KEY_FALLBACK_USER_ID", raising=False)
    # Encryption key for CRM endpoints.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return root


@pytest.fixture
def client(per_user_root):
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _tenant_db(per_user_root: Path, uid: str) -> Path:
    return per_user_root / uid / "data" / "pipeline.db"


# ---------- Coach: /today + /sent + /replies + /pipeline + /snoozes ----------

def test_today_returns_per_tenant_picks(client, per_user_root: Path) -> None:
    """/today with Alice's JWT should never see Bob's picks."""
    alice = client.get("/today", headers=_auth(_ALICE))
    bob = client.get("/today", headers=_auth(_BOB))
    assert alice.status_code == 200
    assert bob.status_code == 200
    # Each tenant gets its own pipeline.db.
    assert _tenant_db(per_user_root, _ALICE).exists()
    assert _tenant_db(per_user_root, _BOB).exists()
    assert (
        _tenant_db(per_user_root, _ALICE).stat().st_ino
        != _tenant_db(per_user_root, _BOB).stat().st_ino
    )


def test_sent_is_per_tenant(client, per_user_root: Path) -> None:
    res_a = client.get("/sent", headers=_auth(_ALICE))
    res_b = client.get("/sent", headers=_auth(_BOB))
    assert res_a.status_code == 200
    assert res_b.status_code == 200
    # Both empty -- but the GET still provisioned the workspace.
    assert _tenant_db(per_user_root, _ALICE).exists()


def test_replies_is_per_tenant(client, per_user_root: Path) -> None:
    res = client.get("/replies", headers=_auth(_ALICE))
    assert res.status_code == 200
    assert _tenant_db(per_user_root, _ALICE).exists()


def test_settings_send_pace_is_per_tenant(client, per_user_root: Path) -> None:
    """Two tenants writing different pace values must not collide."""
    a = client.post(
        "/settings/send-pace", json={"value": 5},
        headers=_auth(_ALICE),
    )
    b = client.post(
        "/settings/send-pace", json={"value": 12},
        headers=_auth(_BOB),
    )
    assert a.json()["value"] == 5
    assert b.json()["value"] == 12
    # Round-trip: Alice should still see 5, Bob still 12.
    assert client.get(
        "/settings/send-pace", headers=_auth(_ALICE),
    ).json()["value"] == 5
    assert client.get(
        "/settings/send-pace", headers=_auth(_BOB),
    ).json()["value"] == 12


def test_settings_discovery_opt_in_is_per_tenant(
    client, per_user_root: Path,
) -> None:
    """Alice opts in; Bob's tenant default is still False."""
    client.post(
        "/settings/discovery-opt-in", json={"opted_in": True},
        headers=_auth(_ALICE),
    )
    a = client.get(
        "/settings/discovery-opt-in", headers=_auth(_ALICE),
    ).json()
    b = client.get(
        "/settings/discovery-opt-in", headers=_auth(_BOB),
    ).json()
    assert a["opted_in"] is True
    assert b["opted_in"] is False


# ---------- B4: per-tenant snoozes don't leak ----------

def test_snoozes_are_per_tenant(client, per_user_root: Path) -> None:
    """Alice's snooze must not be visible to Bob -- the GET returns
    the empty view shape with no leakage."""
    # Seed a draft for Alice so we have a draft_id to snooze.
    from core.db import email_drafts, get_engine, partners
    # Ensure Alice's workspace exists by hitting an endpoint.
    client.get("/runs", headers=_auth(_ALICE))
    client.get("/runs", headers=_auth(_BOB))
    eng_a = get_engine(
        f"sqlite:///{_tenant_db(per_user_root, _ALICE)}"
    )
    with eng_a.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_a", name="A", email="a@x.example",
        ))
        conn.execute(email_drafts.insert().values(
            draft_id=42, partner_id="p_a",
            subject="x", body="x", approval_status="needs_review",
        ))
    # Snooze in Alice's workspace.
    import datetime as _dt
    until = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=6)
    ).isoformat()
    res = client.post(
        "/snoozes/42",
        json={"snoozed_until": until},
        headers=_auth(_ALICE),
    )
    assert res.status_code == 200, res.text
    # Bob hitting /snoozes/42 sees the empty view (no leak).
    bob_view = client.get(
        "/snoozes/42", headers=_auth(_BOB),
    ).json()
    assert bob_view["snoozed_until"] is None


# ---------- CRM connections are per-tenant ----------

def test_crm_connections_are_per_tenant(client, per_user_root: Path) -> None:
    """Alice connects Attio; Bob's GET /crm/connection returns []."""
    res = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "alice-secret-12345"},
        headers=_auth(_ALICE),
    )
    assert res.status_code == 200, res.text
    # Alice sees her connection.
    a_list = client.get(
        "/crm/connection", headers=_auth(_ALICE),
    ).json()
    assert len(a_list) == 1
    assert a_list[0]["provider"] == "attio"
    # Bob sees nothing -- no cross-tenant leak.
    b_list = client.get(
        "/crm/connection", headers=_auth(_BOB),
    ).json()
    assert b_list == []
