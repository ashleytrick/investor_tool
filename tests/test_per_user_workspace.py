"""Tests for Phase 2a -- per-user workspace resolution.

The interesting paths:

  - Toggle off (`WORKSPACE_PER_USER` unset / false): every request,
    JWT or legacy, hits the single pinned `INVESTOR_WORKSPACE`. This
    is the test default + every pre-cutover deployment.

  - Toggle on + a principal is in the contextvar:
    `_engine_and_ws()` returns `(engine, ws)` for
    `${WORKSPACES_ROOT}/${user_id}/` -- auto-provisioned from
    `${WORKSPACE_TEMPLATE}` on first auth.

  - Toggle on but no principal in the contextvar: falls back to
    the pinned path. Tests that don't pass auth get the legacy
    behavior.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------- helpers ----------

def _reload_deps():
    """Force a fresh import of web.deps so env-var changes via
    monkeypatch are picked up by the module's getter helpers."""
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    return deps


# ---------- toggle / opt-in ----------

def test_per_user_routing_off_by_default(monkeypatch, workspace: Path) -> None:
    """Without WORKSPACE_PER_USER, even an authenticated request
    lands on INVESTOR_WORKSPACE. This protects every existing
    test + pre-Phase-2a deployment from accidental migration."""
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.delenv("WORKSPACE_PER_USER", raising=False)
    deps = _reload_deps()

    # Simulate a request where current_principal has run + set the
    # user_id contextvar.
    token = deps._CURRENT_USER_ID_VAR.set(
        "11111111-1111-1111-1111-111111111111"
    )
    try:
        _, ws = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(token)
    assert str(ws.path).rstrip("/") == str(workspace).rstrip("/")


def test_per_user_routing_off_keeps_pinned_path_even_when_principal_set(
    monkeypatch, workspace: Path,
) -> None:
    """Same property, restated: toggle is the controlling switch.
    A principal in the contextvar with toggle off is ignored."""
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("WORKSPACE_PER_USER", "false")
    deps = _reload_deps()
    token = deps._CURRENT_USER_ID_VAR.set("any-user")
    try:
        _, ws = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(token)
    assert str(ws.path).rstrip("/") == str(workspace).rstrip("/")


# ---------- toggle on + principal set ----------

def test_per_user_routing_provisions_workspace_on_first_auth(
    tmp_path: Path, monkeypatch,
) -> None:
    """Toggle on + a fresh user_id -> workspace gets provisioned
    from WORKSPACE_TEMPLATE under WORKSPACES_ROOT/{user_id}/."""
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()

    target_uuid = "22222222-2222-2222-2222-222222222222"
    target_dir = tmp_path / "workspaces" / target_uuid
    assert not target_dir.exists()

    token = deps._CURRENT_USER_ID_VAR.set(target_uuid)
    try:
        _, ws = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(token)

    # Workspace exists + config files came from the template + the
    # DB is fresh (the template's pipeline.db is intentionally not
    # carried over).
    assert target_dir.exists()
    assert (target_dir / "config" / "company.yaml").exists()
    # Engine open creates the schema; the file is present + non-zero.
    db = target_dir / "data" / "pipeline.db"
    assert db.exists()


def test_per_user_routing_is_idempotent_on_existing_workspace(
    tmp_path: Path, monkeypatch,
) -> None:
    """Second request for the same user_id finds the existing
    workspace -- no copytree, no DB wipe. The first request's data
    survives."""
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()

    target_uuid = "33333333-3333-3333-3333-333333333333"

    # First request: provisions the workspace.
    tok1 = deps._CURRENT_USER_ID_VAR.set(target_uuid)
    try:
        _, ws1 = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok1)

    # Write a marker file into the workspace's data dir.
    marker = ws1.path / "data" / "raw" / "user_marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("alive", encoding="utf-8")

    # Second request: reuses the same workspace, marker still there.
    tok2 = deps._CURRENT_USER_ID_VAR.set(target_uuid)
    try:
        _, ws2 = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok2)
    assert ws2.path == ws1.path
    assert (ws2.path / "data" / "raw" / "user_marker.txt").read_text() == "alive"


def test_per_user_routing_isolates_workspaces_by_user_id(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two different user_ids land on two different workspace
    paths -- filesystem-level tenant isolation."""
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()

    uid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    uid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    tok = deps._CURRENT_USER_ID_VAR.set(uid_a)
    try:
        _, ws_a = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok)

    tok = deps._CURRENT_USER_ID_VAR.set(uid_b)
    try:
        _, ws_b = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok)

    assert ws_a.path != ws_b.path
    assert uid_a in str(ws_a.path)
    assert uid_b in str(ws_b.path)


# ---------- toggle on but no principal ----------

def test_per_user_routing_falls_back_to_pinned_when_no_principal(
    monkeypatch, workspace: Path, tmp_path: Path,
) -> None:
    """Even with the toggle on, requests that didn't authenticate
    (no contextvar value) get the pinned INVESTOR_WORKSPACE. This
    is what happens for the public root health-check route + any
    legacy single-tenant fallback."""
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()
    # Contextvar is clear (autouse fixture in conftest ran).
    assert deps._CURRENT_USER_ID_VAR.get() is None
    _, ws = deps._engine_and_ws()
    assert str(ws.path).rstrip("/") == str(workspace).rstrip("/")


# ---------- path safety ----------

def test_per_user_path_rejects_traversal(
    monkeypatch, tmp_path: Path, workspace: Path,
) -> None:
    """user_id is a path component -- a malicious or buggy token
    with `..` or `/` in `sub` must NOT escape WORKSPACES_ROOT.

    Pinning INVESTOR_WORKSPACE so the legacy fallback (which
    triggers only for an EMPTY user_id, not a bad slug) has a
    valid target; the test is specifically about the slug check
    rejecting non-empty bad shapes.
    """
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()

    from fastapi import HTTPException
    for bad in (
        "../etc/passwd",
        "../../foo",
        "abc/def",
        "user with spaces",   # whitespace inside a slug
        "has.dots",           # period not in the allowed set
    ):
        tok = deps._CURRENT_USER_ID_VAR.set(bad)
        try:
            with pytest.raises(HTTPException) as ei:
                deps._engine_and_ws()
            assert ei.value.status_code == 400, (
                f"bad slug {bad!r} should 400, got {ei.value.status_code}"
            )
        finally:
            deps._CURRENT_USER_ID_VAR.reset(tok)


def test_per_user_path_accepts_supabase_uuid_shape(
    monkeypatch, tmp_path: Path,
) -> None:
    """Supabase emits standard UUIDs (alnum + dashes). All shapes
    pass the slug check."""
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("WORKSPACE_TEMPLATE", "clients/test_workspace")
    deps = _reload_deps()
    good = "44444444-4444-4444-4444-444444444444"
    tok = deps._CURRENT_USER_ID_VAR.set(good)
    try:
        _, ws = deps._engine_and_ws()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok)
    assert good in str(ws.path)


def test_missing_template_raises_500(
    monkeypatch, tmp_path: Path,
) -> None:
    """If WORKSPACE_TEMPLATE points at a non-existent path, the
    operator sees a clean 500 instead of silently getting an empty
    workspace that breaks downstream queries."""
    monkeypatch.setenv("WORKSPACE_PER_USER", "true")
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv(
        "WORKSPACE_TEMPLATE", str(tmp_path / "does_not_exist"),
    )
    deps = _reload_deps()
    from fastapi import HTTPException

    tok = deps._CURRENT_USER_ID_VAR.set(
        "55555555-5555-5555-5555-555555555555"
    )
    try:
        with pytest.raises(HTTPException) as ei:
            deps._engine_and_ws()
        assert ei.value.status_code == 500
        assert "template" in str(ei.value.detail).lower()
    finally:
        deps._CURRENT_USER_ID_VAR.reset(tok)


# ---------- contextvar wiring through current_principal ----------

def test_current_principal_populates_contextvar(monkeypatch) -> None:
    """Phase 2a wiring: when current_principal authenticates a
    JWT, it sets the request-scoped contextvar so _engine_and_ws()
    sees the user_id without each endpoint having to thread it
    through. Pairs with the autouse-reset fixture in conftest."""
    import time
    import jwt as pyjwt
    secret = "phase-2a-cv-test-secret"
    monkeypatch.setenv("SUPABASE_JWT_SECRET", secret)
    deps = _reload_deps()
    target = "77777777-7777-7777-7777-777777777777"
    token = pyjwt.encode(
        {
            "sub": target,
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "role": "authenticated",
        },
        secret, algorithm="HS256",
    )
    # Pre-condition: contextvar is clear (autouse reset).
    assert deps._CURRENT_USER_ID_VAR.get() is None
    principal = deps.current_principal(
        authorization=f"Bearer {token}",
    )
    assert principal is not None
    assert principal["user_id"] == target
    # Side effect: contextvar is set.
    assert deps._CURRENT_USER_ID_VAR.get() == target
