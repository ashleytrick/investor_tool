"""B4: tests for pipeline-stage + snooze endpoints.

Covers GET/POST /partners/{partner_id}/pipeline,
GET/POST/DELETE /snoozes/{draft_id}, and the validators
(future-only snoozes, unknown partner / draft -> 404).
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path

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
    # Build schema + seed one partner and one draft so the endpoints
    # have valid IDs to operate on.
    from core.db import (
        email_drafts, get_engine, partners,
    )
    engine = get_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_alice", name="Alice", email="alice@example.com",
        ))
        conn.execute(email_drafts.insert().values(
            draft_id=42, partner_id="p_alice",
            subject="Re: intro", body="hello",
            approval_status="needs_review",
        ))
    return dst


@pytest.fixture
def client(workspace: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def _future_iso(minutes: int = 60) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=minutes)
    ).isoformat()


# ---------- pipeline ----------

def test_get_pipeline_returns_empty_view_when_no_row(client) -> None:
    res = client.get("/partners/p_alice/pipeline", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    assert body["partner_id"] == "p_alice"
    assert body["stage"] is None
    assert body["notes"] is None


def test_get_pipeline_returns_empty_for_unknown_partner(client) -> None:
    """The endpoint deliberately doesn't 404 -- the Today/review
    pages call this for every card and 404 noise is unhelpful."""
    res = client.get("/partners/p_doesnt_exist/pipeline", headers=_auth())
    assert res.status_code == 200
    assert res.json()["stage"] is None


def test_set_pipeline_round_trips(client) -> None:
    res = client.post(
        "/partners/p_alice/pipeline",
        json={"stage": "researching", "notes": "Checking thesis fit."},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["stage"] == "researching"
    assert body["notes"] == "Checking thesis fit."
    assert body["updated_at"]

    # Round-trip: GET returns the same payload.
    res2 = client.get("/partners/p_alice/pipeline", headers=_auth())
    assert res2.json()["stage"] == "researching"
    assert res2.json()["notes"] == "Checking thesis fit."


def test_set_pipeline_overwrites_prior_stage(client) -> None:
    client.post(
        "/partners/p_alice/pipeline", json={"stage": "sent"}, headers=_auth(),
    )
    res = client.post(
        "/partners/p_alice/pipeline",
        json={"stage": "replied", "notes": "Got an interested reply."},
        headers=_auth(),
    )
    assert res.json()["stage"] == "replied"
    # Confirm only one row exists.
    from core.db import get_engine, partner_pipeline
    from sqlalchemy import select, func
    eng = get_engine(
        f"sqlite:///{Path(client.app.dependency_overrides).parent}/data/pipeline.db"
    ) if False else None
    # Use the env-pointed workspace directly instead.
    import os
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(partner_pipeline)
        ).scalar()
    assert n == 1


def test_set_pipeline_404_for_unknown_partner(client) -> None:
    res = client.post(
        "/partners/p_does_not_exist/pipeline",
        json={"stage": "researching"},
        headers=_auth(),
    )
    assert res.status_code == 404


def test_set_pipeline_rejects_empty_stage(client) -> None:
    res = client.post(
        "/partners/p_alice/pipeline", json={"stage": ""}, headers=_auth(),
    )
    assert res.status_code == 422


def test_pipeline_requires_auth(client) -> None:
    assert client.get("/partners/p_alice/pipeline").status_code == 401
    assert client.post(
        "/partners/p_alice/pipeline", json={"stage": "sent"},
    ).status_code == 401


def test_pipeline_sources_endpoint_still_routes_correctly(client) -> None:
    """Sanity: the existing POST /pipeline/sources must keep working.
    We moved B4's pipeline-stage endpoints to /partners/{id}/pipeline
    specifically to avoid this collision; this test pins the
    contract."""
    res = client.post(
        "/pipeline/sources", headers=_auth(),
    )
    assert res.status_code in {400, 422}


# ---------- snoozes ----------

def test_get_snooze_returns_empty_when_none(client) -> None:
    res = client.get("/snoozes/42", headers=_auth())
    assert res.status_code == 200
    assert res.json() == {
        "draft_id": 42, "snoozed_until": None,
        "reason": None, "created_at": None,
    }


def test_set_snooze_round_trips(client) -> None:
    until = _future_iso(60)
    res = client.post(
        "/snoozes/42",
        json={"snoozed_until": until, "reason": "wait for funding announcement"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_id"] == 42
    assert body["reason"] == "wait for funding announcement"
    # ISO comparison: server may return microsecond precision; just
    # ensure same prefix to seconds.
    assert body["snoozed_until"].startswith(until[:19])

    # Round-trip via GET.
    res2 = client.get("/snoozes/42", headers=_auth())
    assert res2.json()["reason"] == "wait for funding announcement"


def test_set_snooze_rejects_past_datetime(client) -> None:
    past = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=10)
    ).isoformat()
    res = client.post(
        "/snoozes/42", json={"snoozed_until": past}, headers=_auth(),
    )
    assert res.status_code == 422


def test_set_snooze_rejects_unknown_draft(client) -> None:
    res = client.post(
        "/snoozes/9999",
        json={"snoozed_until": _future_iso()},
        headers=_auth(),
    )
    assert res.status_code == 404


def test_set_snooze_overwrites_prior_value(client) -> None:
    client.post(
        "/snoozes/42",
        json={"snoozed_until": _future_iso(60), "reason": "first"},
        headers=_auth(),
    )
    client.post(
        "/snoozes/42",
        json={"snoozed_until": _future_iso(120), "reason": "second"},
        headers=_auth(),
    )
    body = client.get("/snoozes/42", headers=_auth()).json()
    assert body["reason"] == "second"


def test_delete_snooze_clears(client) -> None:
    client.post(
        "/snoozes/42",
        json={"snoozed_until": _future_iso(60)},
        headers=_auth(),
    )
    res = client.delete("/snoozes/42", headers=_auth())
    assert res.status_code == 200
    # Double-delete 404s.
    assert client.delete("/snoozes/42", headers=_auth()).status_code == 404
    # GET returns the empty view again.
    body = client.get("/snoozes/42", headers=_auth()).json()
    assert body["snoozed_until"] is None


def test_snoozes_require_auth(client) -> None:
    assert client.get("/snoozes/42").status_code == 401
    assert client.post(
        "/snoozes/42", json={"snoozed_until": _future_iso()},
    ).status_code == 401
    assert client.delete("/snoozes/42").status_code == 401


def test_set_snooze_accepts_z_suffix_iso(client) -> None:
    """ISO with `Z` (Zulu) suffix is the common JS Date.toISOString()
    shape; the server normalizes it via fromisoformat."""
    until = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    res = client.post(
        "/snoozes/42", json={"snoozed_until": until}, headers=_auth(),
    )
    assert res.status_code == 200, res.text
