"""Smoke + auth + happy-path tests for the FastAPI backend.

Goal of this file: catch the obvious classes of regression the
external frontend would hit -- not exhaustive coverage of every
edge case (the underlying CLIs + core/* modules have their own
tests).

What we assert here:
  - the auth header is required and validated
  - the 8 documented endpoints exist + return shapes the frontend
    expects (OpenAPI spec is generated from the same models)
  - a real approve flow through the API mutates the DB the same
    way the CLI does (because we shell out to the CLI)
  - CORS preflight returns the configured allow-list
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


@pytest.fixture
def workspace_with_one_pending_draft(tmp_path: Path) -> Path:
    """Build a fixture workspace and run Stage 7 so there's at least
    one pending-review draft for the API to surface."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    _run(
        "07_generate_emails.py", "--workspace", str(ws_dst),
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )
    return ws_dst


@pytest.fixture
def client(workspace_with_one_pending_draft: Path, monkeypatch) -> TestClient:
    """Construct a TestClient pinned to our fixture workspace + a
    deterministic API key. The app reads env at request time so
    monkeypatching here is enough."""
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv(
        "INVESTOR_WORKSPACE", str(workspace_with_one_pending_draft),
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    # Import lazily so the CORS middleware picks up our env var.
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    return TestClient(api_mod.app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


# ---------- auth ----------

def test_root_health_check_is_public(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "investor-outreach-api"
    assert body["status"] == "ok"


def test_pending_review_requires_bearer(client: TestClient) -> None:
    res = client.get("/review/pending")
    assert res.status_code == 401
    assert "missing bearer token" in res.text.lower()


def test_pending_review_rejects_wrong_key(client: TestClient) -> None:
    res = client.get(
        "/review/pending",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert res.status_code == 401
    assert "invalid api key" in res.text.lower()


def test_pending_review_accepts_correct_bearer(client: TestClient) -> None:
    res = client.get("/review/pending", headers=_auth_headers())
    assert res.status_code == 200


# ---------- read endpoints ----------

def test_pending_review_returns_drafts_with_gate(client: TestClient) -> None:
    res = client.get("/review/pending", headers=_auth_headers())
    assert res.status_code == 200
    drafts = res.json()
    assert len(drafts) > 0
    sample = drafts[0]
    assert "draft_id" in sample
    assert "partner_id" in sample
    assert "subject" in sample
    assert "body" in sample
    assert "gate" in sample
    # Gate shape matches what the frontend types against.
    gate = sample["gate"]
    assert "ok" in gate
    assert "blockers" in gate
    assert "overridden" in gate
    if gate["blockers"]:
        b = gate["blockers"][0]
        assert b["severity"] in {"hard", "soft"}
        assert "text" in b


def test_approved_drafts_empty_when_nothing_approved(client: TestClient) -> None:
    res = client.get("/drafts/approved", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == []


def test_runs_returns_list(client: TestClient) -> None:
    res = client.get("/runs", headers=_auth_headers())
    assert res.status_code == 200
    rows = res.json()
    assert isinstance(rows, list)
    # Stage 1-7 each emit a run row.
    assert len(rows) >= 6


def test_check_ready_returns_phase_result(client: TestClient) -> None:
    res = client.get(
        "/check_ready?phase=review", headers=_auth_headers(),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["phase"] == "review"
    assert isinstance(body["blocked"], bool)
    assert isinstance(body["stdout"], str)


def test_check_ready_rejects_unknown_phase(client: TestClient) -> None:
    res = client.get(
        "/check_ready?phase=NOPE", headers=_auth_headers(),
    )
    assert res.status_code == 422  # FastAPI validation


# ---------- mutation flow ----------

def test_set_email_then_approve_round_trips_through_cli(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """End-to-end: pick a pending draft, set its partner email via the
    API, approve it via the API, and assert the SQLite row reflects
    the approval. Proves the subprocess wiring + the workspace lock +
    the audit log all flow through the API the same way they do
    through the CLI."""
    # Pick a draft.
    pending = client.get("/review/pending", headers=_auth_headers())
    draft = pending.json()[0]
    draft_id = draft["draft_id"]
    pid = draft["partner_id"]

    # Set email.
    res = client.post(
        f"/partners/{pid}/email",
        headers=_auth_headers(),
        json={"email": "api-test@op.example"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True

    # Approve.
    res = client.post(
        f"/drafts/{draft_id}/approve",
        headers=_auth_headers(),
        json={"notes": "via API test"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True

    # DB reflects the approval.
    db = workspace_with_one_pending_draft / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    status = c.execute(
        "select approval_status from email_drafts where draft_id=?",
        (draft_id,),
    ).fetchone()[0]
    c.close()
    assert status == "approved_to_send"

    # Approved-queue endpoint now sees it.
    approved = client.get("/drafts/approved", headers=_auth_headers())
    assert any(d["draft_id"] == draft_id for d in approved.json())


def test_approve_required_notes_is_enforced_by_pydantic(client: TestClient) -> None:
    pending = client.get("/review/pending", headers=_auth_headers())
    draft_id = pending.json()[0]["draft_id"]
    res = client.post(
        f"/drafts/{draft_id}/approve",
        headers=_auth_headers(),
        json={"notes": ""},
    )
    assert res.status_code == 422


def test_approve_propagates_cli_refusal_as_400(client: TestClient) -> None:
    """A draft with no partner email triggers the gate's HARD refusal;
    the API should propagate that as 400 with the CLI's stdout so the
    frontend can show the operator exactly why."""
    pending = client.get("/review/pending", headers=_auth_headers())
    draft_id = pending.json()[0]["draft_id"]
    res = client.post(
        f"/drafts/{draft_id}/approve",
        headers=_auth_headers(),
        json={"notes": "should refuse"},
    )
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert "partner email is unknown" in detail["stdout"].lower()


# ---------- export ----------

def test_send_queue_csv_downloads_when_approved_drafts_exist(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    # Approve one first.
    pending = client.get("/review/pending", headers=_auth_headers())
    draft = pending.json()[0]
    client.post(
        f"/partners/{draft['partner_id']}/email",
        headers=_auth_headers(),
        json={"email": "csv-test@op.example"},
    )
    client.post(
        f"/drafts/{draft['draft_id']}/approve",
        headers=_auth_headers(),
        json={"notes": "csv export"},
    )
    res = client.get("/send_queue.csv", headers=_auth_headers())
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    body = res.text
    assert "draft_id" in body
    assert "partner_email" in body
    assert str(draft["draft_id"]) in body


# ---------- CORS ----------

def test_cors_preflight_responds_to_configured_origin(client: TestClient) -> None:
    res = client.options(
        "/review/pending",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert res.status_code in (200, 204)
    assert (
        res.headers.get("access-control-allow-origin")
        == "https://app.example.com"
    )


# ---------- OpenAPI spec for Lovable ----------

def test_openapi_spec_lists_all_8_documented_endpoints(client: TestClient) -> None:
    """The frontend generates its types from /openapi.json. Refuse a
    regression that removes a documented endpoint."""
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})
    for required in (
        "/review/pending",
        "/drafts/approved",
        "/drafts/{draft_id}/approve",
        "/drafts/{draft_id}/reject",
        "/partners/{partner_id}/email",
        "/check_ready",
        "/runs",
        "/send_queue.csv",
    ):
        assert required in paths, (
            f"OpenAPI spec dropped {required}; "
            f"frontend regen will lose the endpoint"
        )
