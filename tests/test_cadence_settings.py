"""FR-2: cadence settings + touches endpoints.

Covers:
  - GET /settings/cadence (seeds Standard preset on first read)
  - PUT /settings/cadence (replace settings + touches)
  - POST /settings/cadence/preset (apply named preset)
  - POST /settings/cadence/pause (flip pause flag)
"""
from __future__ import annotations

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


# ---------- GET defaults ----------

def test_get_cadence_seeds_standard_preset_on_first_read(client) -> None:
    res = client.get("/settings/cadence", headers=_auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["enabled"] is True
    assert body["paused"] is False
    assert body["max_touches"] == 4
    assert body["daily_mix_new_pct"] == 60
    assert body["auto_stop_on_reply"] is True
    # Standard preset: t2/t3/t4 with new_signal/specific_ask/graceful_close.
    assert len(body["touches"]) == 3
    angles = [t["angle"] for t in body["touches"]]
    assert angles == ["new_signal", "specific_ask", "graceful_close"]


def test_get_cadence_requires_auth(client) -> None:
    assert client.get("/settings/cadence").status_code == 401


# ---------- PUT (replace) ----------

def test_put_cadence_replaces_settings_and_touches(client) -> None:
    body = {
        "enabled": True,
        "paused": False,
        "max_touches": 3,
        "daily_mix_new_pct": 80,
        "auto_stop_on_reply": True,
        "auto_stop_on_pipeline_advance": True,
        "auto_stop_on_manual_pass": True,
        "auto_stop_on_fund_news": True,
        "touches": [
            {"position": 2, "gap_days": 4, "angle": "new_signal"},
            {"position": 3, "gap_days": 9, "angle": "graceful_close"},
        ],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["max_touches"] == 3
    assert out["daily_mix_new_pct"] == 80
    assert out["auto_stop_on_fund_news"] is True
    assert len(out["touches"]) == 2
    assert out["touches"][0]["gap_days"] == 4

    # Re-read confirms persistence.
    out2 = client.get("/settings/cadence", headers=_auth()).json()
    assert out2 == out


def test_put_cadence_rejects_invalid_angle(client) -> None:
    body = {
        "max_touches": 2,
        "touches": [
            {"position": 2, "gap_days": 3, "angle": "barbecue"},
        ],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 422


def test_put_cadence_rejects_position_above_max(client) -> None:
    body = {
        "max_touches": 2,
        "touches": [
            {"position": 5, "gap_days": 3, "angle": "new_signal"},
        ],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 422


def test_put_cadence_rejects_position_below_2(client) -> None:
    body = {
        "max_touches": 3,
        "touches": [
            {"position": 1, "gap_days": 3, "angle": "new_signal"},
        ],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 422


def test_put_cadence_rejects_duplicate_positions(client) -> None:
    body = {
        "max_touches": 4,
        "touches": [
            {"position": 2, "gap_days": 3, "angle": "new_signal"},
            {"position": 2, "gap_days": 7, "angle": "specific_ask"},
        ],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 422


def test_put_cadence_clamps_daily_mix_new_pct(client) -> None:
    """0-100 range enforced by Pydantic."""
    body = {
        "max_touches": 3,
        "daily_mix_new_pct": 150,
        "touches": [],
    }
    res = client.put("/settings/cadence", json=body, headers=_auth())
    assert res.status_code == 422


# ---------- presets ----------

def test_preset_aggressive(client) -> None:
    res = client.post(
        "/settings/cadence/preset",
        json={"preset": "aggressive"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["max_touches"] == 4
    assert body["daily_mix_new_pct"] == 50
    assert len(body["touches"]) == 3
    # t2/t3/t4 angles for aggressive: new_signal -> specific_ask -> graceful_close
    angles = [t["angle"] for t in body["touches"]]
    assert angles == ["new_signal", "specific_ask", "graceful_close"]
    # gap_days match the spec (2 / 4 / 7).
    gaps = [t["gap_days"] for t in body["touches"]]
    assert gaps == [2, 4, 7]


def test_preset_patient_has_5_touches(client) -> None:
    res = client.post(
        "/settings/cadence/preset",
        json={"preset": "patient"},
        headers=_auth(),
    )
    body = res.json()
    assert body["max_touches"] == 5
    assert body["daily_mix_new_pct"] == 70
    assert len(body["touches"]) == 4  # touches 2..5


def test_preset_unknown_value_rejected(client) -> None:
    res = client.post(
        "/settings/cadence/preset",
        json={"preset": "extremely_aggressive"},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_preset_preserves_pause_and_auto_stop_flags(client) -> None:
    """Flipping a preset shouldn't reset the operator's pause or
    auto-stop preferences."""
    # Pause + flip auto_stop_on_fund_news first.
    client.put(
        "/settings/cadence",
        json={
            "max_touches": 3,
            "paused": True,
            "auto_stop_on_fund_news": True,
            "touches": [
                {"position": 2, "gap_days": 3, "angle": "new_signal"},
            ],
        },
        headers=_auth(),
    )
    res = client.post(
        "/settings/cadence/preset",
        json={"preset": "standard"},
        headers=_auth(),
    )
    body = res.json()
    assert body["paused"] is True  # preserved
    assert body["auto_stop_on_fund_news"] is True  # preserved
    # Touches replaced by the preset.
    angles = [t["angle"] for t in body["touches"]]
    assert angles == ["new_signal", "specific_ask", "graceful_close"]


# ---------- pause ----------

def test_pause_round_trips(client) -> None:
    res = client.post(
        "/settings/cadence/pause",
        json={"paused": True},
        headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["paused"] is True
    # Unpause.
    res2 = client.post(
        "/settings/cadence/pause",
        json={"paused": False},
        headers=_auth(),
    )
    assert res2.json()["paused"] is False


def test_pause_does_not_alter_touches(client) -> None:
    """Just flips the boolean; touches survive."""
    # First seed standard.
    client.get("/settings/cadence", headers=_auth())  # triggers default
    client.put(
        "/settings/cadence",
        json={
            "max_touches": 3,
            "touches": [
                {"position": 2, "gap_days": 5, "angle": "soft_check_in"},
            ],
        },
        headers=_auth(),
    )
    client.post(
        "/settings/cadence/pause",
        json={"paused": True},
        headers=_auth(),
    )
    body = client.get("/settings/cadence", headers=_auth()).json()
    assert body["paused"] is True
    assert len(body["touches"]) == 1
    assert body["touches"][0]["angle"] == "soft_check_in"


def test_endpoints_require_auth(client) -> None:
    assert client.put("/settings/cadence", json={
        "max_touches": 2, "touches": []
    }).status_code == 401
    assert client.post(
        "/settings/cadence/preset", json={"preset": "standard"},
    ).status_code == 401
    assert client.post(
        "/settings/cadence/pause", json={"paused": True},
    ).status_code == 401
