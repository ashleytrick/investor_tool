"""Tests for Phase 6 -- POST /gmail/bootstrap.

Supabase admin API is mocked end-to-end; no test hits the network.
The Gmail token persistence is verified by reading the resulting
file on disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


_USER_UUID = "11111111-1111-1111-1111-111111111111"
_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"


# ---------- core.supabase_admin.fetch_google_identity ----------

def _identity_response(
    refresh_token: str | None = "rt-abc",
    access_token: str | None = "at-abc",
    scope: str | object = _GMAIL_SCOPE,
    email: str = "ashley@kismet.fund",
) -> dict:
    """Build a Supabase admin API response body shaped like
    `GET /auth/v1/admin/users/{id}` returns. The Google identity
    is one entry in `identities[]`."""
    identity_data: dict = {"email": email}
    if access_token is not None:
        identity_data["provider_token"] = access_token
    if refresh_token is not None:
        identity_data["provider_refresh_token"] = refresh_token
    if scope is not None:
        identity_data["scopes"] = scope
    return {
        "id": _USER_UUID,
        "email": email,
        "identities": [{
            "provider": "google",
            "identity_data": identity_data,
        }],
    }


class _StubResp:
    """httpx.Client.get returns this in tests so we don't need a
    real server."""
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def test_fetch_google_identity_extracts_provider_tokens(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa.httpx if hasattr(sa, "httpx") else __import__("httpx"),
        "Client",
        autospec=False,
    ) as _MockClient:
        # Build a stand-in client that's used in a `with` block.
        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def get(self, *args, **kwargs):
                return _StubResp(200, _identity_response())
        _MockClient.return_value = _Client()
        ident = sa.fetch_google_identity(_USER_UUID)
    assert ident is not None
    assert ident.email == "ashley@kismet.fund"
    assert ident.provider_access_token == "at-abc"
    assert ident.provider_refresh_token == "rt-abc"
    assert ident.scopes == [_GMAIL_SCOPE]


def test_fetch_google_identity_returns_none_without_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    assert sa.fetch_google_identity(_USER_UUID) is None


def test_fetch_google_identity_returns_none_for_non_google_user(
    monkeypatch,
) -> None:
    """User authenticated with email/password only -- no Google
    identity -> None."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    body = {
        "id": _USER_UUID,
        "email": "u@x.example",
        "identities": [
            {"provider": "email", "identity_data": {}},
        ],
    }
    with patch("httpx.Client") as _MockClient:
        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def get(self, *args, **kwargs):
                return _StubResp(200, body)
        _MockClient.return_value = _Client()
        ident = sa.fetch_google_identity(_USER_UUID)
    assert ident is None


def test_fetch_google_identity_parses_scope_list_or_string(
    monkeypatch,
) -> None:
    """Supabase sometimes returns scopes as a space-delimited
    string, sometimes as a list. Both shapes must work."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    # String shape.
    with patch("httpx.Client") as _MockClient:
        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def get(self, *args, **kwargs):
                return _StubResp(
                    200,
                    _identity_response(
                        scope="openid email "
                              "https://www.googleapis.com/auth/gmail.send",
                    ),
                )
        _MockClient.return_value = _Client()
        ident = sa.fetch_google_identity(_USER_UUID)
    assert ident is not None
    assert _GMAIL_SCOPE in ident.scopes
    assert "openid" in ident.scopes

    # List shape.
    sa.clear_cache()
    with patch("httpx.Client") as _MockClient:
        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def get(self, *args, **kwargs):
                return _StubResp(
                    200,
                    _identity_response(
                        scope=["openid", "email", _GMAIL_SCOPE],
                    ),
                )
        _MockClient.return_value = _Client()
        ident = sa.fetch_google_identity(_USER_UUID)
    assert ident is not None
    assert _GMAIL_SCOPE in ident.scopes


# ---------- /gmail/bootstrap endpoint ----------

def _client(workspace: Path, monkeypatch):
    """FastAPI TestClient with API_KEY auth resolving to
    `_USER_UUID` as the principal."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt")
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.setenv("API_KEY_FALLBACK_USER_ID", _USER_UUID)
    monkeypatch.setenv(
        "SUPABASE_GOOGLE_CLIENT_ID", "google-client-id",
    )
    monkeypatch.setenv(
        "SUPABASE_GOOGLE_CLIENT_SECRET", "google-client-secret",
    )
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app), api_mod


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def test_bootstrap_happy_path_persists_token(
    monkeypatch, workspace: Path,
) -> None:
    """Supabase returns a valid Google identity with the right
    scope -> the endpoint writes .gmail_token.json + returns
    {connected: true, email: ...}."""
    client, _ = _client(workspace, monkeypatch)
    from core import supabase_admin as sa
    with patch.object(
        sa, "fetch_google_identity",
        return_value=sa.GoogleIdentity(
            email="ashley@kismet.fund",
            provider_access_token="at-1",
            provider_refresh_token="rt-1",
            scopes=[_GMAIL_SCOPE],
        ),
    ):
        res = client.post(
            "/gmail/bootstrap", headers=_auth_headers(),
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "connected": True,
        "email": "ashley@kismet.fund",
    }
    # Token landed at the workspace's standard path.
    token_path = workspace / ".gmail_token.json"
    assert token_path.exists()
    stored = json.loads(token_path.read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "rt-1"
    assert stored["token"] == "at-1"
    assert stored["client_id"] == "google-client-id"
    assert stored["client_secret"] == "google-client-secret"
    assert _GMAIL_SCOPE in stored["scopes"]
    assert stored["token_uri"] == "https://oauth2.googleapis.com/token"


def test_bootstrap_missing_refresh_token_409(
    monkeypatch, workspace: Path,
) -> None:
    """Spec: 409 missing_refresh_token -- frontend falls back to
    /gmail/connect."""
    client, _ = _client(workspace, monkeypatch)
    from core import supabase_admin as sa
    with patch.object(
        sa, "fetch_google_identity",
        return_value=sa.GoogleIdentity(
            email="u@x.example",
            provider_access_token="at-1",
            provider_refresh_token=None,  # missing
            scopes=[_GMAIL_SCOPE],
        ),
    ):
        res = client.post(
            "/gmail/bootstrap", headers=_auth_headers(),
        )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["error"] == "missing_refresh_token"


def test_bootstrap_no_google_identity_at_all_409(
    monkeypatch, workspace: Path,
) -> None:
    """User has no Google identity in Supabase -> 409 (same path
    as missing-refresh-token; the frontend falls back the same
    way)."""
    client, _ = _client(workspace, monkeypatch)
    from core import supabase_admin as sa
    with patch.object(
        sa, "fetch_google_identity", return_value=None,
    ):
        res = client.post(
            "/gmail/bootstrap", headers=_auth_headers(),
        )
    assert res.status_code == 409


def test_bootstrap_insufficient_scope_403(
    monkeypatch, workspace: Path,
) -> None:
    """Spec: 403 insufficient_scope when the granted scopes don't
    cover Gmail draft creation. The frontend should re-request
    OAuth with the right scope."""
    client, _ = _client(workspace, monkeypatch)
    from core import supabase_admin as sa
    with patch.object(
        sa, "fetch_google_identity",
        return_value=sa.GoogleIdentity(
            email="u@x.example",
            provider_access_token="at-1",
            provider_refresh_token="rt-1",
            scopes=["openid", "email"],  # no gmail scope
        ),
    ):
        res = client.post(
            "/gmail/bootstrap", headers=_auth_headers(),
        )
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert detail["error"] == "insufficient_scope"
    assert "gmail.send" in str(detail["required_one_of"]).lower()


def test_bootstrap_accepts_broader_scope_subsuming_compose(
    monkeypatch, workspace: Path,
) -> None:
    """An operator who signed up with a broader Gmail scope (e.g.
    https://mail.google.com/) shouldn't need to re-consent narrower
    -- the broader scope subsumes compose."""
    client, _ = _client(workspace, monkeypatch)
    from core import supabase_admin as sa
    for broader in (
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
    ):
        with patch.object(
            sa, "fetch_google_identity",
            return_value=sa.GoogleIdentity(
                email="u@x.example",
                provider_access_token="at-1",
                provider_refresh_token="rt-1",
                scopes=[broader],
            ),
        ):
            res = client.post(
                "/gmail/bootstrap", headers=_auth_headers(),
            )
        assert res.status_code == 200, (broader, res.text)


def test_bootstrap_missing_supabase_google_client_credentials_500(
    monkeypatch, workspace: Path,
) -> None:
    """If the operator hasn't deployed
    SUPABASE_GOOGLE_CLIENT_ID/SECRET, the backend can't refresh
    the harvested token. Bail with a clear 500."""
    client, _ = _client(workspace, monkeypatch)
    monkeypatch.delenv("SUPABASE_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("SUPABASE_GOOGLE_CLIENT_SECRET", raising=False)
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    from core import supabase_admin as sa
    with patch.object(
        sa, "fetch_google_identity",
        return_value=sa.GoogleIdentity(
            email="u@x.example",
            provider_access_token="at-1",
            provider_refresh_token="rt-1",
            scopes=[_GMAIL_SCOPE],
        ),
    ):
        res = client.post(
            "/gmail/bootstrap", headers=_auth_headers(),
        )
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["error"] == "supabase_google_client_unconfigured"


def test_bootstrap_requires_auth(monkeypatch, workspace: Path) -> None:
    client, _ = _client(workspace, monkeypatch)
    res = client.post("/gmail/bootstrap")
    assert res.status_code == 401
