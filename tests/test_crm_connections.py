"""B5: tests for CRM connection foundation.

Covers crm_secrets encrypt/decrypt round-trip, suffix display,
fail-closed when CRM_ENCRYPTION_KEY is unset, and the FastAPI
endpoints (GET /crm/connection, POST /crm/connect, DELETE
/crm/connection).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _gen_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


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
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", _gen_key())
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


# ---------- core/crm_secrets.py ----------

def test_encrypt_decrypt_round_trip(monkeypatch) -> None:
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", _gen_key())
    from core.crm_secrets import decrypt_api_key, encrypt_api_key
    ct = encrypt_api_key("secret-attio-pat-12345")
    assert ct != "secret-attio-pat-12345"  # encrypted
    assert decrypt_api_key(ct) == "secret-attio-pat-12345"


def test_encrypt_misconfigured_when_key_unset(monkeypatch) -> None:
    monkeypatch.delenv("CRM_ENCRYPTION_KEY", raising=False)
    from core.crm_secrets import CRMSecretsMisconfigured, encrypt_api_key
    with pytest.raises(CRMSecretsMisconfigured):
        encrypt_api_key("any")


def test_encrypt_misconfigured_on_bad_key_shape(monkeypatch) -> None:
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    from core.crm_secrets import CRMSecretsMisconfigured, encrypt_api_key
    with pytest.raises(CRMSecretsMisconfigured):
        encrypt_api_key("any")


def test_key_suffix_returns_last_four(monkeypatch) -> None:
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", _gen_key())
    from core.crm_secrets import key_suffix
    assert key_suffix("abcdefghijkl") == "ijkl"
    assert key_suffix("xy") == "xy"  # shorter than 4 returns the whole string
    assert key_suffix("") == ""


# ---------- /crm/connection (list) ----------

def test_get_connections_empty_when_none(client) -> None:
    res = client.get("/crm/connection", headers=_auth())
    assert res.status_code == 200
    assert res.json() == []


def test_get_connections_requires_auth(client) -> None:
    assert client.get("/crm/connection").status_code == 401


# ---------- POST /crm/connect ----------

def test_connect_attio_round_trips(client) -> None:
    res = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "attio-pat-supersecret-abcd"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "attio"
    assert body["key_suffix"] == "abcd"
    assert "encrypted_api_key" not in body  # never leaked
    assert "api_key" not in body  # never leaked
    assert body["last_sync_status"] == "idle"

    # GET shows it.
    listed = client.get("/crm/connection", headers=_auth()).json()
    assert len(listed) == 1
    assert listed[0]["provider"] == "attio"
    assert listed[0]["key_suffix"] == "abcd"


def test_connect_overwrites_prior_key(client) -> None:
    """Re-connecting the same provider rotates the stored key."""
    client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "old-key-1111"},
        headers=_auth(),
    )
    res = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "new-key-2222"},
        headers=_auth(),
    )
    assert res.json()["key_suffix"] == "2222"
    listed = client.get("/crm/connection", headers=_auth()).json()
    assert len(listed) == 1  # still one row
    assert listed[0]["key_suffix"] == "2222"


def test_connect_rejects_unsupported_provider(client) -> None:
    res = client.post(
        "/crm/connect",
        json={"provider": "myspace", "api_key": "key-12345678"},
        headers=_auth(),
    )
    assert res.status_code == 422
    assert "unsupported" in res.text.lower()


def test_connect_rejects_short_api_key(client) -> None:
    res = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "short"},
        headers=_auth(),
    )
    assert res.status_code == 422  # Pydantic min_length=8


def test_connect_requires_auth(client) -> None:
    res = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "key-12345678"},
    )
    assert res.status_code == 401


def test_connect_500_when_encryption_key_unset(workspace, monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.delenv("CRM_ENCRYPTION_KEY", raising=False)
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    res = c.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "key-12345678"},
        headers=_auth(),
    )
    assert res.status_code == 500
    assert "CRM_ENCRYPTION_KEY" in res.text


def test_stored_ciphertext_decrypts_to_original(client) -> None:
    """Round-trip through the API + the DB: store via POST, fetch
    the ciphertext directly from the workspace DB, decrypt, get
    the same plaintext back."""
    import os
    plaintext = "rotation-test-key-9999"
    client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": plaintext},
        headers=_auth(),
    )
    from core.crm_secrets import decrypt_api_key
    from core.db import crm_connections, get_engine
    from sqlalchemy import select
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        row = conn.execute(
            select(crm_connections).where(
                crm_connections.c.provider == "attio"
            )
        ).first()
    assert decrypt_api_key(row.encrypted_api_key) == plaintext


# ---------- DELETE /crm/connection ----------

def test_disconnect_removes_row(client) -> None:
    client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "key-12345678"},
        headers=_auth(),
    )
    res = client.delete(
        "/crm/connection?provider=attio", headers=_auth(),
    )
    assert res.status_code == 200, res.text
    listed = client.get("/crm/connection", headers=_auth()).json()
    assert listed == []


def test_disconnect_404_when_nothing_to_remove(client) -> None:
    res = client.delete(
        "/crm/connection?provider=attio", headers=_auth(),
    )
    assert res.status_code == 404


def test_disconnect_rejects_unsupported_provider(client) -> None:
    res = client.delete(
        "/crm/connection?provider=myspace", headers=_auth(),
    )
    assert res.status_code == 422


def test_disconnect_requires_auth(client) -> None:
    res = client.delete("/crm/connection?provider=attio")
    assert res.status_code == 401
