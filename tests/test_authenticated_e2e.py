"""Review item #17: full authenticated per-user E2E.

One test that walks the happy path Lovable's frontend takes a
new operator through, from JWT auth through to creating Gmail
drafts. It runs in WORKSPACE_PER_USER mode against a real
provisioned tenant directory and asserts state at every step.

Goal: regression net for the cross-feature interactions PR #75
(per-user routing), B1-B9 (Coach + CRM), and the review items
(#8 wizard ingest, #11 opt-in, #21 extraction_failed, #22
admin skipped) all shipped this session. If any one of those
PRs silently breaks the others, this test catches it.

Network is fully mocked:
  - LLM stays in stub mode (no ANTHROPIC_API_KEY)
  - Gmail / CRM HTTP layers aren't reached (no tokens / api_keys
    on disk for the test tenant)

The test does NOT cover Gmail OAuth (that needs a separate fake
Google server) or real Stage runs (covered by the existing
test_pipeline_e2e). It covers the AUTH-AUTHORIZATION-ROUTING
seams and the per-user data isolation that the production
deployment depends on.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
import time
from pathlib import Path

import jwt as _pyjwt
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


_JWT_SECRET = "e2e-jwt-secret-32-bytes-long-x"
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


@pytest.fixture
def e2e_env(tmp_path: Path, monkeypatch):
    """Per-user workspace mode + JWT auth + CRM encryption key
    + hook secret -- everything the prod-shape happy-path needs."""
    root = tmp_path / "workspaces"
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
    monkeypatch.setenv("HOOK_SECRET", "e2e-hook-secret")
    monkeypatch.setenv("API_KEY", "unused-but-required")
    monkeypatch.delenv("AUTH_ALLOW_API_KEY_FALLBACK", raising=False)
    monkeypatch.delenv("API_KEY_FALLBACK_USER_ID", raising=False)
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CRM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return root


@pytest.fixture
def client(e2e_env):
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_jwt(uid)}"}


# ---------- the big test ----------

def test_full_authenticated_per_user_e2e(client, e2e_env: Path) -> None:
    """One operator (Alice) walks the wizard from cold start
    through to a snoozed reply. Bob runs in parallel and stays
    isolated."""
    alice_root = e2e_env / _ALICE
    bob_root = e2e_env / _BOB

    # Step 1: Auth + workspace provisioning.
    r = client.get("/runs", headers=_auth(_ALICE))
    assert r.status_code == 200
    assert alice_root.is_dir(), "Alice's workspace should be auto-provisioned"

    r = client.get("/runs", headers=_auth(_BOB))
    assert r.status_code == 200
    assert bob_root.is_dir()
    assert alice_root != bob_root

    # Step 2: Default settings are sensible.
    r = client.get("/settings/send-pace", headers=_auth(_ALICE))
    assert r.json() == {"value": 10}
    r = client.get("/settings/discovery-opt-in", headers=_auth(_ALICE))
    assert r.json() == {"opted_in": False}

    # Step 3: Operator picks a tighter pace + opts into discovery pool.
    r = client.post(
        "/settings/send-pace", json={"value": 5},
        headers=_auth(_ALICE),
    )
    assert r.status_code == 200
    r = client.post(
        "/settings/discovery-opt-in", json={"opted_in": True},
        headers=_auth(_ALICE),
    )
    assert r.status_code == 200

    # Step 4: /today is empty (no Stage 7 drafts yet).
    r = client.get("/today", headers=_auth(_ALICE))
    assert r.status_code == 200
    assert r.json() == []

    # Step 5: Operator connects a CRM (encrypted at rest).
    r = client.post(
        "/crm/connect",
        json={"provider": "attio", "api_key": "alice-attio-supersecret"},
        headers=_auth(_ALICE),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "attio"
    assert body["key_suffix"] == "cret"
    assert "api_key" not in body
    assert "encrypted_api_key" not in body

    # Step 6: Bob never sees Alice's CRM connection.
    bob_crm = client.get(
        "/crm/connection", headers=_auth(_BOB),
    ).json()
    assert bob_crm == []

    # Step 7: Today queue still empty for Bob.
    assert client.get("/today", headers=_auth(_BOB)).json() == []

    # Step 8: Seed Alice's workspace with one partner + one draft so
    # the review/today flow has data. We inject directly into the
    # tenant's DB because the alternative -- a full Stage 1-7 run --
    # is the test_pipeline_e2e's job.
    from core.db import (
        email_drafts, get_engine, partners, partner_score_summaries,
    )
    alice_db = alice_root / "data" / "pipeline.db"
    eng = get_engine(f"sqlite:///{alice_db}")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_zara", name="Zara Khan",
            email="zara@northbeam.example",
        ))
        conn.execute(email_drafts.insert().values(
            draft_id=101, partner_id="p_zara",
            subject="quick intro: round closing in 3 weeks",
            body="Hi Zara -- saw your Bessemer talk last month...",
            approval_status="needs_review",
        ))
        conn.execute(partner_score_summaries.insert().values(
            partner_id="p_zara",
            send_now_priority=0.92,
            recommendation_reasoning=(
                "sector overlap (fintech) + recent thesis post on "
                "embedded-finance startups"
            ),
            scored_at=_dt.datetime.now(_dt.timezone.utc),
        ))

    # Step 9: /today now surfaces the seeded draft with rationale.
    r = client.get("/today", headers=_auth(_ALICE))
    assert r.status_code == 200
    today = r.json()
    assert len(today) == 1
    pick = today[0]
    assert pick["partner_id"] == "p_zara"
    assert pick["draft_id"] == 101
    assert "fintech" in (pick["rationale"] or "")
    # The hydrated draft carries the rationale on DraftView too (B1).
    assert pick["draft"]["rationale"] is not None

    # Step 10: /review/pending sees the same draft + rationale.
    r = client.get("/review/pending", headers=_auth(_ALICE))
    assert r.status_code == 200
    pending = r.json()
    assert any(d["draft_id"] == 101 for d in pending)
    target = next(d for d in pending if d["draft_id"] == 101)
    assert target["rationale"] is not None

    # Step 11: Operator updates the partner's pipeline stage.
    r = client.post(
        "/partners/p_zara/pipeline",
        json={"stage": "researching", "notes": "checking fund site"},
        headers=_auth(_ALICE),
    )
    assert r.status_code == 200
    r = client.get(
        "/partners/p_zara/pipeline", headers=_auth(_ALICE),
    )
    assert r.json()["stage"] == "researching"

    # Step 12: Operator snoozes the draft for tomorrow.
    tomorrow = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=24)
    ).isoformat()
    r = client.post(
        "/snoozes/101",
        json={"snoozed_until": tomorrow, "reason": "waiting on fund news"},
        headers=_auth(_ALICE),
    )
    assert r.status_code == 200

    # Step 13: /today now drops the snoozed draft.
    r = client.get("/today", headers=_auth(_ALICE))
    assert r.status_code == 200
    assert all(p["draft_id"] != 101 for p in r.json())

    # Step 14: Sent + Replies tabs are still empty (we haven't
    # sent anything yet).
    assert client.get("/sent", headers=_auth(_ALICE)).json() == []
    assert client.get("/replies", headers=_auth(_ALICE)).json() == []

    # Step 15: Cron hook auth -- right secret + JWT-less.
    r = client.post("/api/public/hooks/poll-gmail-sent")
    assert r.status_code == 401
    r = client.post(
        "/api/public/hooks/poll-gmail-sent",
        headers={"X-Hook-Secret": "e2e-hook-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    # Both Alice + Bob workspaces exist on disk now but have no
    # Gmail token -> the per-tenant poll is a no-op.
    assert body["total_inserted"] == 0

    # Step 16: Wizard's "Run pipeline" button (#8). Patch the
    # CLI runner so the test doesn't actually invoke Stages 1-5
    # against the live filesystem; we're checking the orchestration.
    captured: list[str] = []

    class _FakeRes:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(*args, timeout=120):
        captured.append(args[0])
        return _FakeRes()

    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    api_mod._run_cli = fake_run  # type: ignore[attr-defined]
    from fastapi.testclient import TestClient
    fresh_client = TestClient(api_mod.app)
    r = fresh_client.post(
        "/pipeline/ingest", headers=_auth(_ALICE),
    )
    assert r.status_code == 200, r.text
    ingest = r.json()
    assert ingest["ok"] is True
    assert len(ingest["stages"]) == 5
    assert captured == [
        "01_aggregate_sources.py",
        "02_enrich_funds.py",
        "03_mine_activity.py",
        "04_mine_partner_signals.py",
        "05_verify_and_quality.py",
    ]

    # Step 17: Bob's tenant remains untouched throughout -- no
    # cross-leakage of pipeline state, settings, or pipeline rows.
    r = client.get("/settings/send-pace", headers=_auth(_BOB))
    assert r.json() == {"value": 10}, "Bob's send_pace shouldn't see Alice's 5"
    r = client.get(
        "/partners/p_zara/pipeline", headers=_auth(_BOB),
    )
    assert r.json()["stage"] is None, "Bob shouldn't see Alice's pipeline rows"


# ---------- sanity: the legacy single-tenant path still works ----------

def test_legacy_mode_smoke_e2e(tmp_path: Path, monkeypatch) -> None:
    """Pre-Phase-2a deployments use INVESTOR_WORKSPACE + the legacy
    shared API_KEY. Smoke that the same auth + the same endpoints
    still work that way."""
    src = REPO_ROOT / "clients" / "test_workspace"
    ws = tmp_path / "ws"
    shutil.copytree(src, ws)
    db = ws / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    monkeypatch.setenv("API_KEY", "legacy-test-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(ws))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.delenv("WORKSPACE_PER_USER", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)

    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)

    res = c.get(
        "/runs", headers={"Authorization": "Bearer legacy-test-key"},
    )
    assert res.status_code == 200
    res = c.get(
        "/settings/send-pace",
        headers={"Authorization": "Bearer legacy-test-key"},
    )
    assert res.status_code == 200
    assert res.json() == {"value": 10}
