"""FR-1b: POST /investors/capture (QR-flow seed).

Covers:
  - happy path: create new partner + fund (provisional, DNC)
  - dedup on linkedin_url -> already_in_pipeline (no overwrite)
  - channel + source whitelists
  - 422 on empty fields
  - operator note lands on partner.bio
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


def _payload(**overrides) -> dict:
    base = {
        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
        "partner_name": "Jane Doe",
        "firm": "Sequoia Capital",
        "channel": "linkedin",
        "cadence_key": "warm",
        "note": "Met at AI Engineer Summit",
        "source": "qr",
    }
    base.update(overrides)
    return base


# ---------- happy path ----------

def test_capture_creates_partner_and_provisional_fund(client) -> None:
    res = client.post(
        "/investors/capture", json=_payload(), headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "created"
    assert body["name"] == "Jane Doe"
    assert body["firm"] == "Sequoia Capital"
    assert body["channel_pref"] == "linkedin"
    assert body["source"] == "qr"
    assert body["note"] == "Met at AI Engineer Summit"
    assert body["fund_id"].endswith(".unclaimed"), (
        "captured firm should land on a pseudo-domain "
        "(QR doesn't carry the real fund domain)"
    )


def test_capture_marks_partner_dnc_and_provisional(client) -> None:
    """Same treatment as discovery_claim_pseudo_domain -- cold
    outreach must not ship before the operator fills a real
    fund domain."""
    res = client.post(
        "/investors/capture", json=_payload(), headers=_auth(),
    )
    body = res.json()
    import os
    from core.db import get_engine, partners
    from sqlalchemy import select
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        row = conn.execute(
            select(partners).where(
                partners.c.partner_id == body["partner_id"],
            )
        ).first()
    assert row.do_not_contact is True
    assert row.is_provisional is True
    assert row.do_not_contact_source == "capture_pseudo_domain"


def test_capture_stores_note_as_bio(client) -> None:
    res = client.post(
        "/investors/capture", json=_payload(note="Met at TechCrunch"),
        headers=_auth(),
    )
    import os
    from core.db import get_engine, partners
    from sqlalchemy import select
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        row = conn.execute(
            select(partners.c.bio).where(
                partners.c.partner_id == res.json()["partner_id"],
            )
        ).first()
    assert row.bio == "Met at TechCrunch"


# ---------- dedup on linkedin_url ----------

def test_capture_returns_already_in_pipeline_on_dupe(client) -> None:
    first = client.post(
        "/investors/capture", json=_payload(), headers=_auth(),
    )
    assert first.json()["status"] == "created"
    second = client.post(
        "/investors/capture",
        json=_payload(partner_name="Different Name", firm="Different Firm"),
        headers=_auth(),
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "already_in_pipeline"
    # Existing row not overwritten -- the original name stays.
    assert body["name"] == "Jane Doe"
    assert body["partner_id"] == first.json()["partner_id"]


def test_capture_dupe_does_not_create_a_second_partner_row(client) -> None:
    client.post("/investors/capture", json=_payload(), headers=_auth())
    client.post("/investors/capture", json=_payload(), headers=_auth())
    client.post("/investors/capture", json=_payload(), headers=_auth())
    import os
    from core.db import get_engine, partners
    from sqlalchemy import select, func
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(partners)
        ).scalar()
    assert n == 1


# ---------- input validation ----------

def test_capture_rejects_invalid_channel(client) -> None:
    res = client.post(
        "/investors/capture",
        json=_payload(channel="slack"),
        headers=_auth(),
    )
    assert res.status_code == 422


def test_capture_rejects_invalid_source(client) -> None:
    res = client.post(
        "/investors/capture",
        json=_payload(source="webhook"),
        headers=_auth(),
    )
    assert res.status_code == 422


def test_capture_rejects_empty_partner_name(client) -> None:
    res = client.post(
        "/investors/capture",
        json=_payload(partner_name=""),
        headers=_auth(),
    )
    assert res.status_code == 422


def test_capture_rejects_empty_firm(client) -> None:
    res = client.post(
        "/investors/capture",
        json=_payload(firm=""),
        headers=_auth(),
    )
    assert res.status_code == 422


def test_capture_rejects_empty_linkedin_url(client) -> None:
    res = client.post(
        "/investors/capture",
        json=_payload(linkedin_url=""),
        headers=_auth(),
    )
    # Could be 422 from pydantic (empty string still passes
    # min_length=0, so server-side check fires).
    assert res.status_code in {400, 422}


def test_capture_requires_auth(client) -> None:
    assert client.post(
        "/investors/capture", json=_payload(),
    ).status_code == 401


# ---------- defaults ----------

def test_capture_defaults_channel_to_email(client) -> None:
    payload = _payload()
    del payload["channel"]
    res = client.post(
        "/investors/capture", json=payload, headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["channel_pref"] == "email"


def test_capture_defaults_source_to_qr(client) -> None:
    payload = _payload()
    del payload["source"]
    res = client.post(
        "/investors/capture", json=payload, headers=_auth(),
    )
    assert res.status_code == 200
    assert res.json()["source"] == "qr"
