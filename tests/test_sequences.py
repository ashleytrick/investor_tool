"""FR-3: sequences + follow_up_drafts foundation.

Covers:
  - seed-on-capture: /investors/capture creates a sequence row
  - GET /sequences/{partner_id}
  - POST /sequences/{sequence_id}/stop (active -> stopped,
    idempotent on re-stop, reason whitelist)
  - POST /sequences/{sequence_id}/skip (active only, advances
    next_touch_due_at)
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
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
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


def _capture(client, **overrides) -> dict:
    payload = {
        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
        "partner_name": "Jane Doe",
        "firm": "Sequoia Capital",
        "channel": "linkedin",
        "cadence_key": "warm",
        "note": None,
        "source": "qr",
    }
    payload.update(overrides)
    res = client.post(
        "/investors/capture", json=payload, headers=_auth(),
    )
    assert res.status_code == 200, res.text
    return res.json()


# ---------- seed-on-capture ----------

def test_capture_seeds_a_sequence_row(client) -> None:
    captured = _capture(client)
    res = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    )
    assert res.status_code == 200, res.text
    seq = res.json()
    assert seq["partner_id"] == captured["partner_id"]
    assert seq["state"] == "active"
    assert seq["current_touch"] == 1
    assert seq["sequence_id"].startswith("seq_")


def test_capture_dupe_does_not_create_second_sequence(client) -> None:
    """Idempotent capture -> idempotent sequence seed."""
    _capture(client)
    _capture(client)  # dedup hit
    _capture(client)
    import os
    from core.db import get_engine, sequences as _seq
    from sqlalchemy import select, func
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(_seq)
        ).scalar()
    assert n == 1


# ---------- GET /sequences/{partner_id} ----------

def test_get_sequence_404_for_partner_without_one(client) -> None:
    """Stage-2-enriched partners that never went through
    /investors/capture have no sequence row -- 404 is the
    contract."""
    # Insert a partner directly, bypassing capture.
    import os
    from core.db import funds, get_engine, partners
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="acme.com", name="Acme", domain="acme.com",
            is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="acme.com_x", fund_id="acme.com", name="X",
        ))
    res = client.get("/sequences/acme.com_x", headers=_auth())
    assert res.status_code == 404


def test_get_sequence_requires_auth(client) -> None:
    assert client.get("/sequences/anything").status_code == 401


# ---------- POST /sequences/{id}/stop ----------

def test_stop_flips_state_and_records_reason(client) -> None:
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    res = client.post(
        f"/sequences/{seq_id}/stop",
        json={"reason": "user"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "stopped"
    assert body["stopped_reason"] == "user"


def test_stop_rejects_unknown_reason(client) -> None:
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    res = client.post(
        f"/sequences/{seq_id}/stop",
        json={"reason": "ennui"},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_stop_is_idempotent_on_already_stopped(client) -> None:
    """Re-stopping shouldn't error or change the existing
    stopped_reason -- the original signal sticks."""
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    client.post(
        f"/sequences/{seq_id}/stop",
        json={"reason": "reply"},
        headers=_auth(),
    )
    res = client.post(
        f"/sequences/{seq_id}/stop",
        json={"reason": "user"},
        headers=_auth(),
    )
    assert res.status_code == 200
    # The first reason ('reply') is preserved -- the auto-stop
    # signal that fired first wins.
    assert res.json()["stopped_reason"] == "reply"


def test_stop_404_for_unknown_sequence(client) -> None:
    res = client.post(
        "/sequences/seq_nonexistent/stop",
        json={"reason": "user"},
        headers=_auth(),
    )
    assert res.status_code == 404


def test_stop_requires_auth(client) -> None:
    assert client.post(
        "/sequences/seq_abc/stop", json={"reason": "user"},
    ).status_code == 401


# ---------- POST /sequences/{id}/skip ----------

def test_skip_pushes_next_touch_due_at_forward(client) -> None:
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    res = client.post(
        f"/sequences/{seq_id}/skip",
        json={"days": 5},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["next_touch_due_at"] is not None
    # Should be roughly 5 days from now.
    due = _dt.datetime.fromisoformat(body["next_touch_due_at"])
    now = _dt.datetime.now()
    delta = due - now
    assert _dt.timedelta(days=4) < delta < _dt.timedelta(days=6)


def test_skip_does_not_change_current_touch(client) -> None:
    """Skipping is deferral, not advancement -- current_touch
    stays where it was."""
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    client.post(
        f"/sequences/{seq_id}/skip",
        json={"days": 3},
        headers=_auth(),
    )
    after = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()
    assert after["current_touch"] == 1


def test_skip_refuses_on_stopped_sequence(client) -> None:
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    client.post(
        f"/sequences/{seq_id}/stop",
        json={"reason": "user"},
        headers=_auth(),
    )
    res = client.post(
        f"/sequences/{seq_id}/skip",
        json={"days": 3},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_skip_rejects_zero_or_negative(client) -> None:
    captured = _capture(client)
    seq_id = client.get(
        f"/sequences/{captured['partner_id']}", headers=_auth(),
    ).json()["sequence_id"]
    res = client.post(
        f"/sequences/{seq_id}/skip", json={"days": 0},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_skip_on_overdue_sequence_rebases_from_now(client) -> None:
    """P1 audit fix: a sequence with next_touch_due_at 20 days in
    the past, then skipped +3 days, must NOT land 17 days in the
    past (which would re-trigger the row in the Today queue
    immediately). The operator's intent is "defer 3 days from
    now", not "defer 3 days from the missed deadline"."""
    captured = _capture(client)
    partner_id = captured["partner_id"]
    seq_id = client.get(
        f"/sequences/{partner_id}", headers=_auth(),
    ).json()["sequence_id"]
    # Set next_touch_due_at to 20 days in the past, bypassing the
    # endpoint (which won't let us set a past date directly).
    import os
    from core.db import get_engine, sequences
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    past_due = _dt.datetime.utcnow() - _dt.timedelta(days=20)
    with eng.begin() as conn:
        conn.execute(
            sequences.update()
            .where(sequences.c.sequence_id == seq_id)
            .values(next_touch_due_at=past_due)
        )
    res = client.post(
        f"/sequences/{seq_id}/skip",
        json={"days": 3},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    due = _dt.datetime.fromisoformat(body["next_touch_due_at"])
    now = _dt.datetime.utcnow()
    # Must be ~3 days in the FUTURE (not 17 days in the past).
    assert due > now, f"skip on overdue must land in future; got {due!r}"
    delta = due - now
    assert _dt.timedelta(days=2) < delta < _dt.timedelta(days=4), (
        f"skip+3 on overdue should land ~3 days from now; got {delta}"
    )


def test_skip_requires_auth(client) -> None:
    assert client.post(
        "/sequences/seq_abc/skip", json={"days": 3},
    ).status_code == 401
