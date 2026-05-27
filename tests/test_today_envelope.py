"""FR-4: /today returns the new envelope shape.

The list response is replaced by
`{date, send_pace, drafts, next_drafts, total_remaining}` so the
frontend can render the daily batch + a "next up" preview + a
remaining badge without round-tripping.

Touch 2+ follow-ups (sourced from `follow_up_drafts`) will mix
into the same arrays once FR-5 generates them. For now every
pick is touch 1 (initial outreach) and `follow_up` is always None
-- the shape is the contract.
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
def workspace_with_drafts(tmp_path: Path, _scored_workspace_source: Path) -> Path:
    """Reuse the same Stage 7 build pattern as test_today_flow.py."""
    from tests.conftest import _run  # noqa: PLC2701
    dst = tmp_path / "test_workspace"
    shutil.copytree(_scored_workspace_source, dst)
    _run(
        "07_generate_emails.py", "--workspace", str(dst),
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )
    return dst


@pytest.fixture
def client(workspace_with_drafts: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace_with_drafts))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


# ---------- envelope shape ----------

def test_today_response_is_envelope_not_list(client) -> None:
    body = client.get("/today", headers=_auth()).json()
    assert isinstance(body, dict)
    for k in ("date", "send_pace", "drafts", "next_drafts", "total_remaining"):
        assert k in body, f"missing envelope field {k!r}"
    assert isinstance(body["drafts"], list)
    assert isinstance(body["next_drafts"], list)
    assert isinstance(body["total_remaining"], int)


def test_envelope_date_is_today_iso(client) -> None:
    body = client.get("/today", headers=_auth()).json()
    assert body["date"] == _dt.date.today().isoformat()


def test_envelope_send_pace_matches_setting(client) -> None:
    client.post(
        "/settings/send-pace", json={"value": 7}, headers=_auth(),
    )
    body = client.get("/today", headers=_auth()).json()
    assert body["send_pace"] == 7


# ---------- per-pick FR-4 fields ----------

def test_each_pick_has_follow_up_field_default_none(client) -> None:
    """Touch 1 (initial outreach) has follow_up=None. FR-5 will
    populate it on touch-2+ rows."""
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("fixture has no pending drafts")
    for pick in body["drafts"]:
        assert "follow_up" in pick
        assert pick["follow_up"] is None  # all touch 1 for now


def test_each_pick_has_snoozed_until_field(client) -> None:
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("fixture has no pending drafts")
    for pick in body["drafts"]:
        assert "snoozed_until" in pick
        # No active snooze on file -> field is None.
        assert pick["snoozed_until"] is None


# ---------- next_drafts preview ----------

def test_next_drafts_is_preview_of_next_batch(client) -> None:
    """next_drafts shows the picks immediately after `drafts` so
    the operator can see what's coming up. Set send_pace=1 so a
    fixture with >=2 reviewable drafts will populate next_drafts."""
    client.post(
        "/settings/send-pace", json={"value": 1}, headers=_auth(),
    )
    pending = client.get("/review/pending", headers=_auth()).json()
    if len(pending) < 2:
        pytest.skip("need at least 2 reviewable drafts to exercise next_drafts")
    body = client.get("/today", headers=_auth()).json()
    assert len(body["drafts"]) == 1
    assert len(body["next_drafts"]) >= 1
    # The two arrays don't overlap.
    drafts_ids = {p["draft_id"] for p in body["drafts"]}
    next_ids = {p["draft_id"] for p in body["next_drafts"]}
    assert not (drafts_ids & next_ids)


# ---------- total_remaining ----------

def test_total_remaining_counts_partners_beyond_drafts(client) -> None:
    """total_remaining is the count of eligible partners NOT
    currently in the `drafts` array, so the badge can read
    'X more in the pipeline'."""
    client.post(
        "/settings/send-pace", json={"value": 1}, headers=_auth(),
    )
    pending = client.get("/review/pending", headers=_auth()).json()
    if not pending:
        pytest.skip("no reviewable drafts in fixture")
    # Distinct partners with reviewable drafts:
    partner_count = len({d["partner_id"] for d in pending})
    body = client.get("/today", headers=_auth()).json()
    expected_remaining = max(0, partner_count - len(body["drafts"]))
    assert body["total_remaining"] == expected_remaining


def test_total_remaining_zero_on_empty_pool(client) -> None:
    """When there are no reviewable drafts at all, total_remaining
    is 0 and both arrays are empty."""
    # Wipe drafts directly so we have an empty pool.
    import os
    from sqlalchemy import delete
    from core.db import email_drafts, get_engine, today_picks
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        conn.execute(delete(today_picks))
        conn.execute(delete(email_drafts))
    body = client.get("/today", headers=_auth()).json()
    assert body["drafts"] == []
    assert body["next_drafts"] == []
    assert body["total_remaining"] == 0


# ---------- snoozed_until hydration ----------

def test_snoozed_until_surfaces_on_pick_when_set(client) -> None:
    """A draft with an elapsed snooze (past timestamp) is still in
    the queue (snoozes only filter when in the future), and its
    `snoozed_until` field reflects the recorded timestamp so the
    UI can render the snooze history."""
    import os
    from core.db import draft_snoozes, get_engine
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("no pending drafts in fixture")
    target = body["drafts"][0]
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
    past_naive = past.replace(tzinfo=None)
    with eng.begin() as conn:
        conn.execute(draft_snoozes.insert().values(
            draft_id=target["draft_id"],
            snoozed_until=past_naive,
            reason="elapsed",
            created_at=past_naive,
        ))
    refreshed = client.get("/today", headers=_auth()).json()
    refreshed_pick = next(
        (p for p in refreshed["drafts"] if p["draft_id"] == target["draft_id"]),
        None,
    )
    assert refreshed_pick is not None, "draft was filtered when it shouldn't be"
    assert refreshed_pick["snoozed_until"] is not None


# ---------- auth ----------

def test_today_envelope_requires_auth(client) -> None:
    assert client.get("/today").status_code == 401
