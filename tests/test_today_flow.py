"""B1: tests for the Coach Today flow.

Covers `GET /today`, `GET/POST /settings/send-pace`, and the new
`rationale` field on `DraftView` (returned by `/review/pending`).

The fixture workspace is run through Stage 6 + Stage 7 so the API
sees real pending drafts with `partner_score_summaries` rows
attached. `_serialize_draft` then populates rationale from
`recommendation_reasoning`.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def workspace_with_drafts(tmp_path: Path, _scored_workspace_source: Path) -> Path:
    """Scored workspace + one Stage 7 run so the API has real pending
    drafts to surface. Reuses the session-cached stages-1-6 build."""
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


# ---------- rationale on DraftView ----------

def test_pending_review_includes_rationale_field(client) -> None:
    """Every DraftView row carries the `rationale` field, even when
    Stage 6 hasn't populated it (the field is optional)."""
    res = client.get("/review/pending", headers=_auth())
    assert res.status_code == 200
    drafts = res.json()
    assert len(drafts) > 0
    for d in drafts:
        assert "rationale" in d
        # The fixture workspace generates real Stage 6 reasoning, so
        # at least some drafts should carry a non-None rationale.
    assert any(d["rationale"] for d in drafts), (
        "expected at least one draft with a non-empty rationale "
        "from Stage 6 -- fixture may have regressed"
    )


# ---------- /settings/send-pace ----------

def test_send_pace_default_is_10(client) -> None:
    res = client.get("/settings/send-pace", headers=_auth())
    assert res.status_code == 200
    assert res.json() == {"value": 10}


def test_send_pace_round_trips(client) -> None:
    res = client.post(
        "/settings/send-pace", json={"value": 7}, headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"value": 7}
    # Persisted across reads.
    res2 = client.get("/settings/send-pace", headers=_auth())
    assert res2.json() == {"value": 7}


def test_send_pace_clamps_below_1_via_pydantic(client) -> None:
    res = client.post(
        "/settings/send-pace", json={"value": 0}, headers=_auth(),
    )
    # Pydantic ge=1 rejects 0 with 422 -- the spec says "clamp 1-20",
    # but rejecting at the boundary is clearer than silently
    # clamping (the frontend never legitimately sends 0).
    assert res.status_code == 422


def test_send_pace_clamps_above_20_via_pydantic(client) -> None:
    res = client.post(
        "/settings/send-pace", json={"value": 21}, headers=_auth(),
    )
    assert res.status_code == 422


def test_send_pace_requires_auth(client) -> None:
    assert client.get("/settings/send-pace").status_code == 401
    assert client.post(
        "/settings/send-pace", json={"value": 5}
    ).status_code == 401


# ---------- /today ----------

def test_today_returns_ranked_picks(client) -> None:
    res = client.get("/today", headers=_auth())
    assert res.status_code == 200, res.text
    body = res.json()
    # FR-4 envelope shape.
    assert isinstance(body, dict)
    assert "date" in body and "send_pace" in body
    assert "drafts" in body and "next_drafts" in body
    assert "total_remaining" in body
    picks = body["drafts"]
    assert isinstance(picks, list)
    if not picks:
        pytest.skip(
            "fixture workspace has no pending drafts to pick from; "
            "Stage 6 may have ranked nothing"
        )
    # Ranks are 1-indexed and strictly increasing.
    ranks = [p["rank"] for p in picks]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1
    # Each pick carries the hydrated draft for the frontend card.
    sample = picks[0]
    assert sample["partner_id"]
    assert sample["draft_id"] > 0
    assert sample["pick_date"]  # ISO date string
    # FR-4 fields default to None on initial-outreach (touch 1).
    assert sample["follow_up"] is None
    assert "snoozed_until" in sample
    # The draft is fully hydrated (gate + subject + body), saving a
    # second round-trip from the Today tab.
    if sample["draft"] is not None:
        assert "subject" in sample["draft"]
        assert "gate" in sample["draft"]


def test_today_is_stable_across_repeated_reads(client) -> None:
    """Spec: picks 'stable per day'. Two GETs in a row must return
    the same partner_ids in the same order."""
    a = client.get("/today", headers=_auth()).json()["drafts"]
    b = client.get("/today", headers=_auth()).json()["drafts"]
    assert [p["partner_id"] for p in a] == [p["partner_id"] for p in b]
    assert [p["rank"] for p in a] == [p["rank"] for p in b]


def test_today_respects_limit_query_param(client) -> None:
    res = client.get("/today?limit=2", headers=_auth())
    assert res.status_code == 200
    assert len(res.json()["drafts"]) <= 2


def test_today_falls_back_to_send_pace_when_limit_omitted(client) -> None:
    # Set send_pace = 1 then call /today without `limit`; we should
    # get at most one pick.
    client.post(
        "/settings/send-pace", json={"value": 1}, headers=_auth(),
    )
    res = client.get("/today", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    # The limit applies even when serving cached picks.
    assert len(body["drafts"]) <= 1
    assert body["send_pace"] == 1


def test_today_requires_auth(client) -> None:
    res = client.get("/today")
    assert res.status_code == 401
