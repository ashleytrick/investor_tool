"""FR-1 frontend wins: investor status / channel / draft-snooze
alias endpoints.

Covers:
  - PUT /investors/{partner_id}/status (writes partner_pipeline)
  - PUT /investors/{partner_id}/channel (writes partners.channel_pref)
  - POST /drafts/{draft_id}/snooze (alias for /snoozes/{draft_id})
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
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import (
        email_drafts, get_engine, partners,
    )
    eng = get_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_alice", name="Alice",
            email="alice@example.com",
        ))
        conn.execute(email_drafts.insert().values(
            draft_id=101, partner_id="p_alice",
            subject="hi", body="hi",
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


def _future_iso(hours: int = 24) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    ).isoformat()


# ---------- /investors/{id}/status ----------

def test_set_status_writes_partner_pipeline(client) -> None:
    res = client.put(
        "/investors/p_alice/status",
        json={"status": "meeting_set"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["partner_id"] == "p_alice"
    assert body["status"] == "meeting_set"
    # Round-trip via the coach pipeline endpoint that reads the
    # same row.
    pipe = client.get(
        "/partners/p_alice/pipeline", headers=_auth(),
    ).json()
    assert pipe["stage"] == "meeting_set"
    assert pipe["updated_by"] == "ui:status_picker"


def test_set_status_404_for_unknown_partner(client) -> None:
    res = client.put(
        "/investors/p_nonexistent/status",
        json={"status": "passed"},
        headers=_auth(),
    )
    assert res.status_code == 404


def test_set_status_requires_auth(client) -> None:
    assert client.put(
        "/investors/p_alice/status", json={"status": "contacted"},
    ).status_code == 401


def test_set_status_empty_string_rejected_by_pydantic(client) -> None:
    res = client.put(
        "/investors/p_alice/status", json={"status": ""},
        headers=_auth(),
    )
    assert res.status_code == 422


# ---------- /investors/{id}/channel ----------

def test_set_channel_round_trips(client) -> None:
    res = client.put(
        "/investors/p_alice/channel",
        json={"channel_pref": "linkedin"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "partner_id": "p_alice",
        "channel_pref": "linkedin",
    }
    # Read back via partners table.
    import os
    from core.db import get_engine, partners
    from sqlalchemy import select
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        row = conn.execute(
            select(partners.c.channel_pref).where(
                partners.c.partner_id == "p_alice",
            )
        ).first()
    assert row.channel_pref == "linkedin"


def test_set_channel_accepts_email_linkedin_both(client) -> None:
    for value in ("email", "linkedin", "both"):
        res = client.put(
            "/investors/p_alice/channel",
            json={"channel_pref": value},
            headers=_auth(),
        )
        assert res.status_code == 200, (value, res.text)
        assert res.json()["channel_pref"] == value


def test_set_channel_rejects_invalid_value(client) -> None:
    res = client.put(
        "/investors/p_alice/channel",
        json={"channel_pref": "slack"},
        headers=_auth(),
    )
    assert res.status_code == 422
    assert "email" in res.text.lower()


def test_set_channel_404_for_unknown_partner(client) -> None:
    res = client.put(
        "/investors/p_nonexistent/channel",
        json={"channel_pref": "email"},
        headers=_auth(),
    )
    assert res.status_code == 404


# ---------- /drafts/{id}/snooze ----------

def test_snooze_alias_round_trips(client) -> None:
    until = _future_iso(48)
    res = client.post(
        "/drafts/101/snooze",
        json={"until": until, "reason": "waiting on update"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_id"] == 101
    assert body["reason"] == "waiting on update"
    # Snooze persisted via the coach /snoozes path too.
    sn = client.get("/snoozes/101", headers=_auth()).json()
    assert sn["reason"] == "waiting on update"


def test_snooze_alias_clears_with_until_null(client) -> None:
    # Set a snooze first.
    client.post(
        "/drafts/101/snooze",
        json={"until": _future_iso(24)},
        headers=_auth(),
    )
    sn_before = client.get("/snoozes/101", headers=_auth()).json()
    assert sn_before["snoozed_until"] is not None
    # Clear via {until: null}.
    res = client.post(
        "/drafts/101/snooze",
        json={"until": None},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "draft_id": 101,
        "snoozed_until": None,
        "reason": None,
        "created_at": None,
    }
    sn_after = client.get("/snoozes/101", headers=_auth()).json()
    assert sn_after["snoozed_until"] is None


def test_snooze_alias_rejects_past_until(client) -> None:
    past = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    ).isoformat()
    res = client.post(
        "/drafts/101/snooze",
        json={"until": past},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_snooze_alias_404_for_unknown_draft(client) -> None:
    res = client.post(
        "/drafts/9999/snooze",
        json={"until": _future_iso()},
        headers=_auth(),
    )
    assert res.status_code == 404


def test_snooze_alias_requires_auth(client) -> None:
    assert client.post(
        "/drafts/101/snooze", json={"until": _future_iso()},
    ).status_code == 401
