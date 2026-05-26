"""Per-user routing under `Depends(require_auth)`.

Pre-this-PR: only `current_principal` populated `_CURRENT_USER_ID_VAR`,
so most authed endpoints (which only declare `Depends(require_auth)`)
silently fell back to the pinned `INVESTOR_WORKSPACE`. Item #1 + #2
of the post-B5 review.

These tests pin the new contract: with `WORKSPACE_PER_USER=true`,
two different JWTs from two different tenants hit two different
workspace files even on routes that only use `require_auth`.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import jwt as _pyjwt
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


_JWT_SECRET = "test-jwt-secret-32-bytes-long-x"
_USER_ALICE = "11111111-1111-1111-1111-111111111111"
_USER_BOB = "22222222-2222-2222-2222-222222222222"


def _mint_jwt(user_id: str) -> str:
    import time
    return _pyjwt.encode(
        {
            "sub": user_id,
            "aud": "authenticated",
            "email": f"{user_id}@example.com",
            "exp": int(time.time()) + 3600,
        },
        _JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def per_user_root(tmp_path: Path, monkeypatch) -> Path:
    """Set up a `WORKSPACES_ROOT` + template that the deps layer
    will copytree from on first auth."""
    root = tmp_path / "workspaces_root"
    root.mkdir()
    template = tmp_path / "ws_template"
    template_src = REPO_ROOT / "clients" / "test_workspace"
    shutil.copytree(template_src, template)
    # Drop any pre-existing db in the template -- each tenant
    # should start with a fresh DB.
    db = template / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(root))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", str(template))
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_KEY", "unused-but-required-by-some-code-paths")
    # Disable the legacy fallback so JWT is the only auth path.
    monkeypatch.delenv("AUTH_ALLOW_API_KEY_FALLBACK", raising=False)
    monkeypatch.delenv("API_KEY_FALLBACK_USER_ID", raising=False)
    return root


@pytest.fixture
def client(per_user_root: Path):
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_jwt(user_id)}"}


# ---------- the regression these tests pin ----------

def test_require_auth_alone_populates_contextvar_under_per_user_mode(
    client, per_user_root: Path,
) -> None:
    """A route that uses ONLY `Depends(require_auth)` (no
    `current_principal`) must still scope to the JWT's tenant.

    Hitting `/runs` (which uses `require_auth`) twice with two
    different JWTs should provision two separate workspace
    directories on disk."""
    # Both calls should succeed.
    res_a = client.get("/runs", headers=_auth(_USER_ALICE))
    assert res_a.status_code == 200, res_a.text
    res_b = client.get("/runs", headers=_auth(_USER_BOB))
    assert res_b.status_code == 200, res_b.text

    # Workspace directories provisioned per user (not the pinned one).
    assert (per_user_root / _USER_ALICE).is_dir()
    assert (per_user_root / _USER_BOB).is_dir()


def test_ws_path_honors_contextvar_for_shell_outs(
    client, per_user_root: Path, monkeypatch,
) -> None:
    """A mutating endpoint that shells out via `_ws_path()` must
    pass each tenant's own workspace to the CLI. We patch the CLI
    runner to capture the --workspace arg and assert it points
    inside the tenant's directory."""
    captured: list[list[str]] = []

    class _FakeRes:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*args, **kwargs):
        captured.append(list(args))
        return _FakeRes()

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)

    res = client.post(
        "/pipeline/score", headers=_auth(_USER_ALICE),
    )
    assert res.status_code == 200, res.text
    # First arg is the script name; --workspace should be in args.
    flat = " ".join(captured[0])
    assert _USER_ALICE in flat, (
        f"shell-out received the wrong workspace path: {flat}"
    )
    assert "INVESTOR_WORKSPACE" not in flat  # not the env path


def test_tenants_have_isolated_dbs(client, per_user_root: Path) -> None:
    """Hit /runs for two tenants and verify each tenant's DB file
    exists under its own workspace dir (not a shared one)."""
    client.get("/runs", headers=_auth(_USER_ALICE))
    client.get("/runs", headers=_auth(_USER_BOB))
    alice_db = per_user_root / _USER_ALICE / "data" / "pipeline.db"
    bob_db = per_user_root / _USER_BOB / "data" / "pipeline.db"
    assert alice_db.exists()
    assert bob_db.exists()
    # Different files -> different inodes -> isolation by file boundary.
    assert alice_db.stat().st_ino != bob_db.stat().st_ino


def test_legacy_mode_still_uses_pinned_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    """Sanity: with `WORKSPACE_PER_USER` unset, `_ws_path()` still
    returns `INVESTOR_WORKSPACE` so legacy single-tenant deploys
    don't regress."""
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "legacy_ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    monkeypatch.setenv("API_KEY", "legacy-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(dst))
    monkeypatch.delenv("WORKSPACE_PER_USER", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)

    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    res = c.get(
        "/runs", headers={"Authorization": "Bearer legacy-key"},
    )
    assert res.status_code == 200
