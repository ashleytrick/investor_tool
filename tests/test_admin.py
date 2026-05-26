"""Tests for Phase 5 -- admin endpoints + Supabase role lookup.

Three surfaces:
  - `core.supabase_admin.get_user_role` (with the 5-min cache)
  - `require_admin` dependency
  - `/admin/companies` / `/admin/investors` / `/admin/tenants`

Supabase REST is mocked end-to-end -- no test hits the network.
The role cache is cleared between tests via the autouse fixture
below so a previous test's cached role doesn't leak.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


_ADMIN_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_role_cache():
    """Reset the Supabase role cache between tests so a cached
    'admin' role from one test doesn't leak into the next."""
    try:
        from core import supabase_admin as sa
        sa.clear_cache()
    except ImportError:
        pass
    yield


# ---------- core.supabase_admin.get_user_role ----------

def test_get_user_role_returns_role_when_configured(monkeypatch) -> None:
    """Supabase URL + service key set + REST returns a role row ->
    `get_user_role` surfaces the role string."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="admin",
    ) as mock_fetch:
        role = sa.get_user_role(_ADMIN_UUID)
    assert role == "admin"
    mock_fetch.assert_called_once_with(_ADMIN_UUID)


def test_get_user_role_caches_result(monkeypatch) -> None:
    """Second call within TTL hits the cache, not Supabase. Per
    the spec: '5 min in-process cache keyed by user_id'."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="admin",
    ) as mock_fetch:
        first = sa.get_user_role(_ADMIN_UUID)
        second = sa.get_user_role(_ADMIN_UUID)
    assert first == "admin" == second
    # Only one network call despite two get_user_role invocations.
    assert mock_fetch.call_count == 1


def test_get_user_role_caches_none_too(monkeypatch) -> None:
    """A user with NO row in user_roles caches the None result --
    a known-non-admin returns fast without re-asking Supabase
    every request."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value=None,
    ) as mock_fetch:
        first = sa.get_user_role(_USER_UUID)
        second = sa.get_user_role(_USER_UUID)
    assert first is None and second is None
    assert mock_fetch.call_count == 1


def test_invalidate_role_drops_cached_entry(monkeypatch) -> None:
    """`invalidate_role(user_id)` evicts so the next call re-asks
    Supabase. The spec says we invalidate on 401."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa, "_fetch_role_from_supabase",
        side_effect=["admin", "user"],
    ) as mock_fetch:
        assert sa.get_user_role(_ADMIN_UUID) == "admin"
        sa.invalidate_role(_ADMIN_UUID)
        assert sa.get_user_role(_ADMIN_UUID) == "user"
    assert mock_fetch.call_count == 2


def test_get_user_role_returns_none_without_supabase_config(
    monkeypatch,
) -> None:
    """No SUPABASE_URL / SERVICE_ROLE_KEY -> the helper returns
    None without trying to call out. Pre-cutover deployments
    keep working in legacy mode."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    assert sa.get_user_role(_ADMIN_UUID) is None


def test_is_admin_shorthand(monkeypatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="admin",
    ):
        assert sa.is_admin(_ADMIN_UUID) is True
    sa.clear_cache()
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="user",
    ):
        assert sa.is_admin(_USER_UUID) is False
    assert sa.is_admin(None) is False
    assert sa.is_admin("") is False


# ---------- require_admin dependency ----------

def test_require_admin_passes_for_api_key_path(monkeypatch) -> None:
    """Legacy API_KEY auth path resolves to role='admin' (set by
    _api_key_fallback_principal). require_admin recognizes that
    without calling Supabase."""
    from fastapi import HTTPException  # noqa: F401 - import-only check
    from web.deps import require_admin
    principal = {
        "user_id": _ADMIN_UUID, "email": "ops@kismet.fund",
        "role": "admin", "source": "api_key",
    }
    # Should not raise; should return the principal verbatim.
    assert require_admin(principal=principal) is principal


def test_require_admin_passes_for_jwt_admin_via_supabase(
    monkeypatch,
) -> None:
    """JWT path: role from claims is just 'authenticated';
    require_admin upgrades via Supabase user_roles lookup."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    from web.deps import require_admin
    principal = {
        "user_id": _ADMIN_UUID, "email": "a@x.example",
        "role": "authenticated", "source": "jwt",
    }
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="admin",
    ):
        result = require_admin(principal=principal)
    assert result is principal


def test_require_admin_rejects_non_admin_jwt(monkeypatch) -> None:
    """A regular user JWT + no admin row in Supabase -> 403."""
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    import importlib
    from core import supabase_admin as sa
    importlib.reload(sa)
    from fastapi import HTTPException
    from web.deps import require_admin
    principal = {
        "user_id": _USER_UUID, "email": "u@x.example",
        "role": "authenticated", "source": "jwt",
    }
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value="user",
    ):
        with pytest.raises(HTTPException) as ei:
            require_admin(principal=principal)
    assert ei.value.status_code == 403


def test_require_admin_401s_when_no_principal() -> None:
    """No authentication at all -> 401 (not 403). Distinguishes
    'we don't know who you are' from 'you're not an admin'."""
    from fastapi import HTTPException
    from web.deps import require_admin
    with pytest.raises(HTTPException) as ei:
        require_admin(principal=None)
    assert ei.value.status_code == 401


# ---------- /admin/* endpoints ----------

def _build_two_tenant_root(tmp_path: Path) -> Path:
    """Set up `${tmp_path}/workspaces/<uid>/` for two tenants,
    copying the test_workspace fixture into each so they have a
    valid SQLite schema + a company.yaml block."""
    import shutil
    from tests.conftest import REPO_ROOT
    src = REPO_ROOT / "clients" / "test_workspace"
    root = tmp_path / "workspaces"
    root.mkdir()
    for uid, name, email in [
        (_ADMIN_UUID, "Acme", "ashley@kismet.fund"),
        (_USER_UUID, "OtherCo", "bob@example.com"),
    ]:
        dst = root / uid
        shutil.copytree(src, dst)
        # Drop the fixture DB so each tenant starts with a fresh
        # schema -- mirrors the provisioning behavior.
        db = dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        # Personalize the company.yaml so the admin endpoint can
        # tell tenants apart.
        yaml_path = dst / "config" / "company.yaml"
        text = yaml_path.read_text(encoding="utf-8")
        text = text.replace('name: "Tendril"', f'name: "{name}"')
        text = text.replace(
            'founder_email: "dana@tendril.example"',
            f'founder_email: "{email}"',
        )
        yaml_path.write_text(text, encoding="utf-8")
    return root


def _admin_client(workspace: Path, tmp_path: Path, monkeypatch):
    """FastAPI TestClient with two tenant workspaces seeded and
    legacy API_KEY auth (which resolves to admin) so the admin
    endpoints answer."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv(
        "WORKSPACES_ROOT", str(_build_two_tenant_root(tmp_path)),
    )
    monkeypatch.setenv(
        "WORKSPACE_TEMPLATE", "clients/test_workspace",
    )
    monkeypatch.setenv("GLOBAL_DB_PATH", str(tmp_path / "global.db"))
    # Required for the legacy-key admin elevation:
    monkeypatch.setenv(
        "API_KEY_FALLBACK_USER_ID", _ADMIN_UUID,
    )
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt")
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")

    import importlib
    from core import investors_global as ig
    from core import supabase_admin as sa
    importlib.reload(ig)
    importlib.reload(sa)
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def test_admin_companies_lists_all_tenants(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    client = _admin_client(workspace, tmp_path, monkeypatch)
    res = client.get("/admin/companies", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    names = sorted(c["name"] for c in body["companies"])
    assert names == ["Acme", "OtherCo"]
    by_uid = {c["user_id"]: c for c in body["companies"]}
    assert by_uid[_ADMIN_UUID]["user_email"] == "ashley@kismet.fund"
    assert by_uid[_USER_UUID]["user_email"] == "bob@example.com"


def test_admin_tenants_returns_counts(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    client = _admin_client(workspace, tmp_path, monkeypatch)
    res = client.get("/admin/tenants", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    by_uid = {t["user_id"]: t for t in body["tenants"]}
    # Both tenants have company.yaml with a name -> company_count=1.
    assert by_uid[_ADMIN_UUID]["company_count"] == 1
    assert by_uid[_USER_UUID]["company_count"] == 1
    # Fresh DBs -> zero partners + drafts.
    assert by_uid[_ADMIN_UUID]["investor_count"] == 0
    assert by_uid[_USER_UUID]["draft_count"] == 0


def test_admin_investors_joins_with_global_pool(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """Seed a tenant partner that was claimed from a global row;
    /admin/investors should return the partner with the global
    row's enriched fields attached."""
    client = _admin_client(workspace, tmp_path, monkeypatch)

    # Seed the global pool + claim from one of the tenant
    # workspaces so the tenant has a partner with
    # claimed_from_global_id set.
    from core import investors_global as ig
    global_engine = ig.get_global_engine()
    global_id = ig.upsert_investor(global_engine, ig.InvestorRow(
        firm="Northbeam", partner="Priya",
        email="priya@northbeam.example",
        sectors=("fintech", "compliance"),
        stages=("seed",),
        enriched_fields={"thesis": "B2B regtech"},
    ))

    # Open the admin tenant's DB and write the local partner row.
    from datetime import datetime, timezone
    from core.db import funds, get_engine, partners
    ws_dir = (
        tmp_path / "workspaces" / _ADMIN_UUID
    )
    eng = get_engine(f"sqlite:///{ws_dir / 'data' / 'pipeline.db'}")
    now = datetime.now(timezone.utc)
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="northbeam.example", name="Northbeam",
            domain="northbeam.example", last_updated=now,
        ))
        conn.execute(partners.insert().values(
            partner_id="northbeam.example_priya",
            fund_id="northbeam.example", name="Priya",
            claimed_from_global_id=global_id,
            last_updated=now,
        ))

    res = client.get("/admin/investors", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] >= 1
    target = next(
        (i for i in body["investors"]
         if i["partner_id"] == "northbeam.example_priya"),
        None,
    )
    assert target is not None
    assert target["user_id"] == _ADMIN_UUID
    assert target["email"] == "priya@northbeam.example"
    # Global enrichment joined through.
    assert set(target["sectors"]) == {"compliance", "fintech"}
    assert target["claimed_from_global_id"] == global_id
    assert target["global_enriched_fields"]["thesis"] == "B2B regtech"


def test_admin_investors_filter_by_tenant(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """`?tenant=<uid>` scopes the response to one tenant."""
    client = _admin_client(workspace, tmp_path, monkeypatch)
    res = client.get(
        f"/admin/investors?tenant={_ADMIN_UUID}",
        headers=_auth_headers(),
    )
    assert res.status_code == 200
    body = res.json()
    for inv in body["investors"]:
        assert inv["user_id"] == _ADMIN_UUID


def test_admin_endpoints_require_admin_role(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """JWT-authed non-admin (no Supabase admin row) gets 403."""
    client = _admin_client(workspace, tmp_path, monkeypatch)
    # Mint a JWT for a non-admin user, with Supabase returning None.
    import time
    import jwt as pyjwt
    token = pyjwt.encode(
        {
            "sub": _USER_UUID,
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "role": "authenticated",
            "email": "u@x.example",
        },
        "test-jwt", algorithm="HS256",
    )
    from core import supabase_admin as sa
    with patch.object(
        sa, "_fetch_role_from_supabase", return_value=None,
    ):
        res = client.get(
            "/admin/companies",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 403


def test_admin_endpoints_require_auth(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    client = _admin_client(workspace, tmp_path, monkeypatch)
    assert client.get("/admin/companies").status_code == 401
    assert client.get("/admin/investors").status_code == 401
    assert client.get("/admin/tenants").status_code == 401


def test_admin_companies_empty_when_per_user_routing_off(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """Without WORKSPACE_PER_USER=true, there's no multi-tenant
    tree to walk. Admin endpoints return empty rather than
    leaking the single legacy workspace as a fake tenant.

    Sets API_KEY_FALLBACK_USER_ID + SUPABASE_JWT_SECRET +
    AUTH_ALLOW_API_KEY_FALLBACK so the legacy-key auth resolves
    to an admin principal -- otherwise require_admin would 401
    before the empty-list response can land.
    """
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv("GLOBAL_DB_PATH", str(tmp_path / "global.db"))
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-jwt")
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.setenv("API_KEY_FALLBACK_USER_ID", _ADMIN_UUID)
    monkeypatch.delenv("WORKSPACE_PER_USER", raising=False)
    monkeypatch.delenv("WORKSPACES_ROOT", raising=False)

    import importlib
    from core import investors_global as ig
    from core import supabase_admin as sa
    importlib.reload(ig)
    importlib.reload(sa)
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    res = client.get("/admin/companies", headers=_auth_headers())
    assert res.status_code == 200, res.text
    # Review #22 added a `skipped` array; legacy mode = empty.
    assert res.json() == {"companies": [], "count": 0, "skipped": []}
