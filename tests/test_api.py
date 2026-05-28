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
    # Fixture workspaces intentionally use .example domains. The hosted API
    # defaults this bypass off; tests opt in explicitly so API happy paths can
    # exercise approval/export without weakening production defaults.
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
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


def test_cors_regex_matches_ephemeral_preview_origin(
    workspace_with_one_pending_draft: Path, monkeypatch,
) -> None:
    """Use case: a frontend host like Lovable rotates preview URLs
    per session (e.g. https://abcd--<project>.lovableproject.com).
    CORS_ORIGIN_REGEX should let those through without an env-var
    update each time, while a different-shape origin still gets
    refused so the regex isn't an effective wildcard.
    """
    import importlib

    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv(
        "INVESTOR_WORKSPACE", str(workspace_with_one_pending_draft),
    )
    # Empty exact-list + a regex that matches Lovable's pattern.
    monkeypatch.setenv("CORS_ORIGINS", "")
    monkeypatch.setenv(
        "CORS_ORIGIN_REGEX",
        r"https://([a-z0-9-]+--)?abc123\.(lovable\.app|lovableproject\.com)",
    )
    import web.api as api_mod
    importlib.reload(api_mod)
    cli = TestClient(api_mod.app)

    # An ephemeral preview origin matching the project id.
    res = cli.options(
        "/review/pending",
        headers={
            "Origin": "https://session-id-99--abc123.lovableproject.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert res.status_code in (200, 204)
    assert (
        res.headers.get("access-control-allow-origin")
        == "https://session-id-99--abc123.lovableproject.com"
    )

    # A different project id at the same host shape must NOT pass.
    res = cli.options(
        "/review/pending",
        headers={
            "Origin": "https://different-project.lovableproject.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    # Starlette returns 400 (or no allow-origin header) on a CORS
    # preflight that doesn't match. Either shape is a refusal.
    if res.status_code in (200, 204):
        assert res.headers.get("access-control-allow-origin") is None


# ---------- OpenAPI spec for Lovable ----------

def test_openapi_spec_lists_all_documented_endpoints(client: TestClient) -> None:
    """The frontend generates its types from /openapi.json. Refuse a
    regression that removes a documented endpoint.

    Covers every endpoint the Lovable frontend was built against:
      - the 8 original routes
      - the 6 onboarding-wizard routes (Lovable spec v1)
      - the Coach surface (B1 Today / B2 Sent / B3 Replies / B4
        pipeline + snoozes)
      - the CRM surface (B5 foundation + B7-9 hooks + bulk import)
      - the cron-hook surface (Gmail + CRM polling)
      - the discovery + admin + Phase 6 surfaces
      - the review-item adds (#8 ingest, #11 opt-in)
    """
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})
    for required in (
        # ---- Original surface ----
        "/review/pending",
        "/drafts/approved",
        "/drafts/{draft_id}/approve",
        "/drafts/{draft_id}/reject",
        "/partners/{partner_id}/email",
        "/check_ready",
        "/runs",
        "/send_queue.csv",
        # ---- Onboarding wizard surface ----
        "/config",
        "/config/mode",
        "/config/company",
        "/config/company/extract-from-deck",
        "/pipeline/sources",
        "/gmail/status",
        "/gmail/connect",
        # ---- Per-scope Google OAuth + Phase 6 bootstrap ----
        "/google/status",
        "/gmail/bootstrap",
        # ---- Review item #8: umbrella ingest endpoint ----
        # The per-stage triggers (aggregate/enrich/activity/
        # partner-signals/verify/score/generate) are operator-only
        # tools hidden from the public spec by batch I; see
        # test_openapi_spec_hides_dev_only_endpoints below.
        "/pipeline/ingest",
        # ---- B1: Today flow ----
        "/today",
        "/settings/send-pace",
        # ---- Review item #11: discovery-pool opt-in ----
        "/settings/discovery-opt-in",
        # ---- B2 + B3: Sent + Replies tabs ----
        "/sent",
        "/replies",
        "/replies/{event_id}/read",
        # ---- B4: Pipeline + snoozes (post-route-conflict-fix) ----
        "/partners/{partner_id}/pipeline",
        "/snoozes/{draft_id}",
        # ---- Phase 4: discovery surface ----
        "/discovery/matches",
        "/discovery/claim",
        # ---- B5 + B9: CRM connection + bulk import ----
        "/crm/connection",
        "/crm/connect",
        "/crm/bulk-import",
        # ---- Cron-hook surface (Gmail + CRM polling) ----
        "/api/public/hooks/poll-gmail-sent",
        "/api/public/hooks/poll-gmail-replies",
        "/api/public/hooks/reconcile-drafts",
        "/api/public/hooks/poll-crm-activity",
        "/api/public/hooks/poll-crm-pipeline",
        "/api/public/hooks/poll-crm-investors",
        "/api/public/hooks/poll-crm-relationships",
        "/api/public/hooks/poll-crm-lists",
        "/api/public/hooks/poll-crm-deals",
    ):
        assert required in paths, (
            f"OpenAPI spec dropped {required}; "
            f"frontend regen will lose the endpoint"
        )


def test_openapi_spec_hides_dev_only_endpoints(client: TestClient) -> None:
    """Batch I: per-stage pipeline triggers and /admin/* surfaces are
    operator-only tooling. They MUST stay out of /openapi.json so the
    Lovable frontend never generates client code that calls them and
    so the public schema reads as a product surface, not a control
    panel. Inclusion is enforced via include_in_schema=False on each
    route definition; this test guards against a regression where
    someone re-exposes them."""
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})
    for hidden in (
        "/pipeline/aggregate",
        "/pipeline/enrich",
        "/pipeline/activity",
        "/pipeline/partner-signals",
        "/pipeline/verify",
        "/pipeline/score",
        "/pipeline/generate",
        "/admin/companies",
        "/admin/investors",
        "/admin/tenants",
    ):
        assert hidden not in paths, (
            f"{hidden} is a dev-only surface and must not appear in "
            f"the public OpenAPI spec (batch I)"
        )


def test_openapi_today_includes_rationale_on_draft_view(
    client: TestClient,
) -> None:
    """B1 added `rationale` to DraftView. The frontend renders it on
    every Today/review card, so a regression that removes the field
    silently breaks the UI."""
    spec = client.get("/openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})
    draft_view = schemas.get("DraftView")
    assert draft_view is not None, "DraftView schema missing"
    props = draft_view.get("properties", {})
    assert "rationale" in props, (
        "DraftView.rationale (B1) missing -- frontend Today/review "
        "cards lose the 'why this partner' line"
    )


def test_openapi_sent_and_replies_response_shapes(client: TestClient) -> None:
    """SentItem (B2) + ReplyItem (B3) are the contract for the
    Sent and Replies tabs. Refuse silent removal of required fields."""
    spec = client.get("/openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})
    sent_item = schemas.get("SentItem")
    assert sent_item is not None
    sent_props = sent_item.get("properties", {})
    for required in (
        "event_id", "external_id", "thread_id",
        "subject", "occurred_at",
    ):
        assert required in sent_props, f"SentItem missing {required}"

    reply_item = schemas.get("ReplyItem")
    assert reply_item is not None
    reply_props = reply_item.get("properties", {})
    for required in (
        "event_id", "classification", "unread",
        "sender_email", "occurred_at",
    ):
        assert required in reply_props, f"ReplyItem missing {required}"


def test_openapi_extraction_response_has_extraction_failed_flag(
    client: TestClient,
) -> None:
    """Review #21: ExtractionResponse.extraction_failed must be in
    the schema so the frontend can render a clear banner without
    string-parsing the warnings list."""
    spec = client.get("/openapi.json").json()
    schemas = spec.get("components", {}).get("schemas", {})
    extraction = schemas.get("ExtractionResponse")
    assert extraction is not None
    assert "extraction_failed" in extraction.get("properties", {})


def test_admin_results_have_skipped_field() -> None:
    """Review #22: AdminCompaniesResult / AdminInvestorsResult /
    AdminTenantsResult all carry a `skipped` array. The admin
    endpoints are hidden from the public OpenAPI spec (batch I), so
    inspect the Pydantic models directly instead of /openapi.json."""
    from web.routers.admin import (
        AdminCompaniesResult,
        AdminInvestorsResult,
        AdminTenantsResult,
    )
    for model in (
        AdminCompaniesResult, AdminInvestorsResult, AdminTenantsResult,
    ):
        assert "skipped" in model.model_fields, (
            f"{model.__name__}.skipped (review #22) missing"
        )


# ---------- onboarding: /config + /config/mode ----------

def test_config_returns_fixture_mode_and_gmail_false(client: TestClient) -> None:
    res = client.get("/config", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    # The shipped test_workspace declares `mode: fixture` in its
    # company.yaml, and the fixture has no .gmail_credentials.json /
    # token, so gmail_connected must be false.
    assert body["mode"] == "fixture"
    assert body["gmail_connected"] is False
    # Build Session 13: per-scope Drive state. Same fixture has no
    # token at all, so drive_connected is False and google_connected
    # (= gmail && drive) must also be False.
    assert body["drive_connected"] is False
    assert body["google_connected"] is False
    # test_workspace's company.yaml has name="Tendril" + one_liner set,
    # so the wizard sees Step 1 as already complete on the fixture.
    assert body["company_configured"] is True


def test_google_status_returns_per_scope_breakdown(client: TestClient) -> None:
    """The dedicated /google/status endpoint surfaces gmail_connected
    and drive_connected independently so the wizard can distinguish
    'Gmail granted but Drive needs re-consent' from 'neither granted'."""
    res = client.get("/google/status", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "gmail_connected": False,
        "drive_connected": False,
        "google_connected": False,
    }


def test_google_status_requires_auth(client: TestClient) -> None:
    res = client.get("/google/status")
    assert res.status_code == 401


def test_set_mode_flips_company_yaml_and_round_trips(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    yaml_path = workspace_with_one_pending_draft / "config" / "company.yaml"
    before = yaml_path.read_text(encoding="utf-8")
    assert "mode: fixture" in before

    res = client.post(
        "/config/mode",
        headers=_auth_headers(),
        json={"mode": "production"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["returncode"] == 0

    after = yaml_path.read_text(encoding="utf-8")
    assert "mode: production" in after
    assert "mode: fixture" not in after
    # Surrounding YAML / comments must survive the edit.
    assert "PLACEHOLDER fixture data" in after
    assert "Tendril" in after  # rest of company.yaml is intact

    # GET /config reflects the new value.
    snap = client.get("/config", headers=_auth_headers()).json()
    assert snap["mode"] == "production"

    # And flip back works too.
    res = client.post(
        "/config/mode",
        headers=_auth_headers(),
        json={"mode": "fixture"},
    )
    assert res.status_code == 200
    assert "mode: fixture" in yaml_path.read_text(encoding="utf-8")


def test_set_mode_accepts_dry_run(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    yaml_path = workspace_with_one_pending_draft / "config" / "company.yaml"

    res = client.post(
        "/config/mode",
        headers=_auth_headers(),
        json={"mode": "dry_run"},
    )
    assert res.status_code == 200, res.text
    assert "mode: dry_run" in yaml_path.read_text(encoding="utf-8")

    snap = client.get("/config", headers=_auth_headers()).json()
    assert snap["mode"] == "dry_run"


def test_set_mode_rejects_unknown_value(client: TestClient) -> None:
    res = client.post(
        "/config/mode",
        headers=_auth_headers(),
        json={"mode": "live"},
    )
    # Literal validation -> 422 from FastAPI.
    assert res.status_code == 422


def test_set_mode_requires_auth(client: TestClient) -> None:
    res = client.post("/config/mode", json={"mode": "production"})
    assert res.status_code == 401


# ---------- onboarding: /pipeline/score + /pipeline/generate ----------
#
# We don't re-exec the real CLIs here -- the existing pipeline tests
# already cover stages 6 and 7 end-to-end, and re-running them through
# the API would add ~30s per test for zero coverage gain. Instead we
# monkeypatch _run_cli to assert the API dispatches the right command
# with the right flags and shapes the response correctly.

class _FakeRes:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_pipeline_score_shells_out_to_stage_6(
    client: TestClient, monkeypatch,
) -> None:
    calls: list[tuple] = []

    def fake(*args: str, timeout: int = 120):
        calls.append((args, timeout))
        return _FakeRes(0, "score ok\n", "")

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake)

    res = client.post("/pipeline/score", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["returncode"] == 0
    assert "score ok" in body["stdout"]
    # Routed to the right script with the workspace flag.
    args, timeout = calls[0]
    assert args[0] == "06_score_candidates.py"
    assert "--workspace" in args
    assert timeout >= 120  # bumped above the default for slow LLM runs


def test_pipeline_score_propagates_failure_as_400(
    client: TestClient, monkeypatch,
) -> None:
    import web.api as api_mod
    monkeypatch.setattr(
        api_mod, "_run_cli",
        lambda *a, **k: _FakeRes(2, "partial\n", "boom\n"),
    )
    res = client.post("/pipeline/score", headers=_auth_headers())
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "score_candidates failed"
    assert detail["stdout"] == "partial\n"
    assert detail["stderr"] == "boom\n"
    assert detail["returncode"] == 2


def test_pipeline_generate_shells_out_to_stage_7_with_calibration_cap(
    client: TestClient, monkeypatch,
) -> None:
    calls: list[tuple] = []

    def fake(*args: str, timeout: int = 120):
        calls.append(args)
        return _FakeRes(0, "drafted\n", "")

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake)

    res = client.post("/pipeline/generate", headers=_auth_headers())
    assert res.status_code == 200, res.text
    args = calls[0]
    assert args[0] == "07_generate_emails.py"
    # --top is capped at the Gate 5.5 ceiling so onboarding never
    # trips the calibration refusal.
    assert "--top" in args
    assert args[args.index("--top") + 1] == "10"
    # --allow-example-domains is required to draft against the test
    # workspace's .example partner emails; a no-op for real domains.
    assert "--allow-example-domains" in args


# ---------- onboarding: /gmail/status + /gmail/connect ----------

def test_gmail_status_false_when_no_credentials(client: TestClient) -> None:
    res = client.get("/gmail/status", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == {"connected": False}


def test_gmail_connect_rejects_when_credentials_missing(
    client: TestClient,
) -> None:
    """Without .gmail_credentials.json on disk we can't even start
    the OAuth flow. Surface as 400 with the GCP-setup hint in the
    error message so the wizard can route the operator to docs."""
    res = client.post("/gmail/connect", headers=_auth_headers())
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "gmail_credentials.json" in detail["error"]


def test_gmail_connect_returns_auth_url_when_credentials_exist(
    client: TestClient, workspace_with_one_pending_draft: Path,
    monkeypatch,
) -> None:
    """When OAuth client JSON is present, /gmail/connect must return a
    Google authorization URL and stash the flow against its state
    token. We monkeypatch the helper so the test doesn't need a real
    GCP credential file."""
    from core import gmail_oauth as gomod

    captured: dict = {}

    def fake_start_flow(ws, redirect_uri):
        captured["redirect_uri"] = redirect_uri
        captured["ws_path"] = str(ws.path)
        return (
            "https://accounts.google.com/o/oauth2/auth?fake=1",
            "state-token-xyz",
        )

    monkeypatch.setattr(gomod, "start_flow", fake_start_flow)

    res = client.post("/gmail/connect", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["auth_url"].startswith("https://accounts.google.com/")
    # The redirect URI we hand Google must point back at our callback
    # route -- otherwise the operator's browser lands somewhere we
    # can't process the code.
    assert captured["redirect_uri"].endswith("/oauth/gmail/callback")


def test_oauth_callback_persists_token_and_renders_confirmation(
    client: TestClient, workspace_with_one_pending_draft: Path,
    monkeypatch,
) -> None:
    """End-to-end: simulate Google's redirect by hitting the callback
    with a known state+code, verify the helper is called with both
    values, verify the token file lands at the workspace's standard
    path, and verify the HTML confirms the connected email so the
    operator sees a success page."""
    from core import gmail_oauth as gomod

    token_path = workspace_with_one_pending_draft / ".gmail_token.json"

    def fake_complete_flow(state, code, ws):
        assert state == "state-token-xyz"
        assert code == "auth-code-abc"
        # Mirror the real helper -- it writes the token before returning.
        token_path.write_text("{\"token\": \"fake\"}", encoding="utf-8")
        return {"emailAddress": "operator@example.com"}

    monkeypatch.setattr(gomod, "complete_flow", fake_complete_flow)
    # Post-#3 review: callback resolves the workspace via the
    # pending-state map instead of `_engine_and_ws()` so it works
    # in per-user mode (where the redirect has no Bearer header).
    # Stub pending_workspace_path so the test doesn't have to run
    # the real start_flow first.
    monkeypatch.setattr(
        gomod, "pending_workspace_path",
        lambda state: str(workspace_with_one_pending_draft),
    )

    res = client.get(
        "/oauth/gmail/callback"
        "?state=state-token-xyz&code=auth-code-abc",
    )
    assert res.status_code == 200, res.text
    assert "operator@example.com" in res.text
    assert "Gmail linked" in res.text
    assert token_path.exists()


def test_oauth_callback_rejects_missing_code(client: TestClient) -> None:
    res = client.get("/oauth/gmail/callback?state=abc")
    assert res.status_code == 400
    assert "code or state" in res.text.lower()


# ---------- onboarding: /config/company ----------

def test_get_company_returns_existing_test_workspace_profile(
    client: TestClient,
) -> None:
    """The shipped test_workspace has a real `company:` block. The GET
    endpoint must surface its values + fall back through the legacy
    nested keys (target_check_size_usd, current_traction,
    meeting_ask) to the flat shape the UI expects."""
    res = client.get("/config/company", headers=_auth_headers())
    assert res.status_code == 200, res.text
    body = res.json()
    # Direct flat fields.
    assert body["name"] == "Tendril"
    assert body["founder_name"] == "Dana Okafor"
    assert body["founder_email"] == "dana@tendril.example"
    assert body["stage"] == "SEED"
    assert "fintech" in body["target_sectors"]
    assert "United States" in body["target_geographies"]
    # Legacy nested -> flat fallback.
    assert body["target_check_min_usd"] == 250000
    assert body["target_check_max_usd"] == 1500000
    assert body["traction"] == "$180K ARR"
    assert body["scheduling_link"] == "https://cal.example/dana-tendril"
    # Fields the fixture doesn't set come back as defaults, not 404'd.
    assert body["problem"] == ""
    assert body["do_not_contact"] == []
    assert body["founded_year"] is None


def test_get_company_on_missing_file_returns_empty_shape(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """A workspace with no company.yaml at all must still return the
    documented shape, not 404 -- the form's controlled inputs need
    something to bind to."""
    (workspace_with_one_pending_draft / "config" / "company.yaml").unlink()
    res = client.get("/config/company", headers=_auth_headers())
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == ""
    assert body["one_liner"] == ""
    assert body["target_sectors"] == []
    assert body["target_check_min_usd"] is None


def test_put_company_writes_block_and_round_trips(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    yaml_path = (
        workspace_with_one_pending_draft / "config" / "company.yaml"
    )
    before = yaml_path.read_text(encoding="utf-8")
    # Make sure the test starts from the fixture content, not whatever
    # an earlier test mutated.
    assert "mode: fixture" in before
    assert "Tendril" in before

    payload = {
        "name": "Acme",
        "one_liner": "B2B compliance API.",
        "website": "https://acme.example",
        "founded_year": 2024,
        "hq_location": "NYC",
        "stage": "Seed",
        "sectors": ["fintech", "compliance"],
        "business_model": "SaaS",
        "problem": "Manual reporting is slow.",
        "solution": "API for reporting.",
        "differentiators": "Built by ex-regulators.",
        "why_now": "New mandates this quarter.",
        "traction": "$200K ARR",
        "round_amount_usd": 3000000,
        "round_instrument": "SAFE",
        "round_valuation_usd": 15000000,
        "round_close_target": "Q1",
        "target_check_min_usd": 250000,
        "target_check_max_usd": 1500000,
        "target_stages": ["seed"],
        "target_sectors": ["fintech"],
        "target_geographies": ["US"],
        "desired_traits": ["leads rounds", "writes first check"],
        "excluded_sectors": ["consumer"],
        "excluded_geographies": ["RU"],
        "do_not_contact": ["evil@example.com"],
        "founder_name": "Jane Founder",
        "founder_title": "CEO",
        "founder_email": "jane@acme.example",
        "signature": "— Jane",
        "tone": "direct",
        "scheduling_link": "https://cal.example/jane",
    }
    res = client.put(
        "/config/company", headers=_auth_headers(), json=payload,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["returncode"] == 0
    assert "Acme" in body["stdout"]

    # Round-trip: GET reflects every field we PUT.
    fetched = client.get("/config/company", headers=_auth_headers()).json()
    for key, want in payload.items():
        assert fetched[key] == want, f"{key} did not round-trip"

    # Sibling blocks and the mode line must be preserved.
    after = yaml_path.read_text(encoding="utf-8")
    assert "mode: fixture" in after
    assert "raise_context:" in after
    assert "founder_voice:" in after
    assert "round_fit:" in after

    # Legacy nested mirrors got populated so the pipeline keeps working.
    assert "target_check_size_usd:" in after
    assert "min: 250000" in after
    assert "max: 1500000" in after
    assert "current_traction:" in after
    assert "headline_metric: $200K ARR" in after
    assert "preferred_scheduling_link: https://cal.example/jane" in after

    # /config snapshot now reports the wizard as configured.
    snap = client.get("/config", headers=_auth_headers()).json()
    assert snap["company_configured"] is True


def test_put_company_with_only_required_fields_is_accepted(
    client: TestClient,
) -> None:
    """Optional fields really are optional -- a half-filled form must
    not 422 just because the operator skipped some questions."""
    res = client.put(
        "/config/company",
        headers=_auth_headers(),
        json={"name": "Bare", "one_liner": "Bare bones."},
    )
    assert res.status_code == 200, res.text
    fetched = client.get("/config/company", headers=_auth_headers()).json()
    assert fetched["name"] == "Bare"
    assert fetched["target_sectors"] == []
    assert fetched["target_check_min_usd"] is None


def test_put_company_requires_auth(client: TestClient) -> None:
    res = client.put("/config/company", json={"name": "X", "one_liner": "Y"})
    assert res.status_code == 401


def test_config_company_configured_false_when_empty(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """company_configured tracks the (name && one_liner) check, so an
    empty profile must surface as not-configured to the wizard."""
    res = client.put(
        "/config/company",
        headers=_auth_headers(),
        json={"name": "", "one_liner": ""},
    )
    assert res.status_code == 200
    snap = client.get("/config", headers=_auth_headers()).json()
    assert snap["company_configured"] is False


# ---------- onboarding: /pipeline/sources upload ----------

_VALID_SOURCES_CSV = (
    b"name,domain\n"
    b"Northbeam Capital,northbeam.example\n"
    b"Tidewater Ventures,tidewater.example\n"
    b"Pier 9 Partners,pier9.example\n"
)


def test_sources_upload_saves_csv_and_wires_yaml(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """Happy path: upload a CSV, server saves it under data/raw/,
    prepends an entry to sources.yaml, and returns row_count so the
    wizard can render 'Loaded N investors'."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": (
            "my_investors.csv", _VALID_SOURCES_CSV, "text/csv",
        )},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["row_count"] == 3
    assert "uploaded" in body["stdout"].lower()

    # File landed under data/raw/.
    csv_path = (
        workspace_with_one_pending_draft / "data" / "raw"
        / "my_investors.csv"
    )
    assert csv_path.exists()
    assert csv_path.read_bytes() == _VALID_SOURCES_CSV

    # sources.yaml now has the upload prepended.
    sources_yaml = (
        workspace_with_one_pending_draft / "config" / "sources.yaml"
    )
    import yaml
    data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))
    first = data["public_lists"][0]
    assert first["path"] == "data/raw/my_investors.csv"
    assert first["parser"] == "csv"
    assert "my_investors" in first["name"]


def test_sources_upload_is_idempotent_on_same_filename(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """Re-uploading the same filename must overwrite the CSV but NOT
    create a duplicate entry in sources.yaml. The operator who
    re-uploads (e.g. corrected typo) shouldn't end up with two
    parallel sources of truth."""
    files = {"file": (
        "investors.csv", _VALID_SOURCES_CSV, "text/csv",
    )}
    client.post(
        "/pipeline/sources", headers=_auth_headers(), files=files,
    )
    # Second upload with the same name + a different body.
    updated = (
        b"name,domain\n"
        b"Only One Fund,only.example\n"
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": ("investors.csv", updated, "text/csv")},
    )
    assert res.status_code == 200
    assert res.json()["row_count"] == 1

    import yaml
    sources_yaml = (
        workspace_with_one_pending_draft / "config" / "sources.yaml"
    )
    data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))
    # Only one entry pointing at this CSV.
    hits = [
        item for item in data["public_lists"]
        if item.get("path") == "data/raw/investors.csv"
    ]
    assert len(hits) == 1


def test_sources_upload_rejects_non_csv_non_xlsx_extension(
    client: TestClient,
) -> None:
    """A .json (or other unsupported) upload returns 400. .xlsx is
    accepted via openpyxl conversion -- see
    test_sources_upload_accepts_xlsx_and_converts_to_csv below."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": ("investors.json", b'{"x":1}', "application/json")},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    # The error message references the supported extensions.
    assert "csv" in detail["error"].lower() or "xlsx" in detail["error"].lower()


def test_sources_upload_rejects_empty_file(client: TestClient) -> None:
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert res.status_code == 400


def test_sources_upload_rejects_header_only_csv(client: TestClient) -> None:
    """A header-only file has zero rows -- Stage 1 would happily run
    but ingest nothing. Refuse upstream so the operator gets a clear
    error rather than wondering why their fund universe is empty."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": ("header_only.csv", b"name,domain\n", "text/csv")},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "no data rows" in detail["error"].lower()


def test_sources_upload_sanitizes_filename(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """A filename with path-traversal characters must NOT land
    outside data/raw/. Sanitization keeps the upload inside the
    workspace boundary."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": (
            "../../etc/passwd.csv", _VALID_SOURCES_CSV, "text/csv",
        )},
    )
    assert res.status_code == 200, res.text
    # Sanitized to a flat alnum stem inside data/raw/.
    raw_dir = workspace_with_one_pending_draft / "data" / "raw"
    csv_files = list(raw_dir.glob("*.csv"))
    assert len(csv_files) >= 1
    # No file landed outside data/raw/.
    outside = workspace_with_one_pending_draft.parent / "etc"
    assert not outside.exists()


def test_sources_upload_requires_auth(client: TestClient) -> None:
    res = client.post(
        "/pipeline/sources",
        files={"file": ("x.csv", _VALID_SOURCES_CSV, "text/csv")},
    )
    assert res.status_code == 401


def _make_xlsx_bytes(rows: list[list[str]]) -> bytes:
    """Generate a minimal .xlsx in-memory for the xlsx upload tests.

    Uses openpyxl to write the same structure an OpenVC export would
    (a single active sheet with a header row and data rows). No
    binary fixtures committed -- the test owns the bytes it
    exercises so a future openpyxl change can't silently break the
    upload path.
    """
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sources_upload_accepts_xlsx_and_converts_to_csv(
    client: TestClient, workspace_with_one_pending_draft: Path,
) -> None:
    """OpenVC's investor export is .xlsx. The endpoint must accept it,
    convert via openpyxl, save under data/raw/ as .csv, and point
    sources.yaml at the .csv so Stage 1's CSV-only parser picks it up
    without an xlsx code path."""
    xlsx_bytes = _make_xlsx_bytes([
        ["Name", "Domain"],
        ["Northbeam Capital", "northbeam.example"],
        ["Tidewater Ventures", "tidewater.example"],
    ])
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": (
            "openvc_export.xlsx", xlsx_bytes,
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet",
        )},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["row_count"] == 2

    # Persisted as .csv (NOT .xlsx) so the workspace contains only
    # one canonical shape for Stage 1 to read.
    raw_dir = workspace_with_one_pending_draft / "data" / "raw"
    csv_files = list(raw_dir.glob("*.csv"))
    xlsx_files = list(raw_dir.glob("*.xlsx"))
    assert len(csv_files) >= 1
    assert len(xlsx_files) == 0, (
        "xlsx upload must be converted; no .xlsx should land in data/raw/"
    )

    # Header row was lowercased so Stage 1's case-sensitive `name` /
    # `domain` lookup matches.
    saved = csv_files[0].read_text(encoding="utf-8")
    assert saved.splitlines()[0] == "name,domain"
    assert "Northbeam Capital,northbeam.example" in saved

    # sources.yaml entry points at the .csv result.
    import yaml
    sources_yaml = (
        workspace_with_one_pending_draft / "config" / "sources.yaml"
    )
    data = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))
    first = data["public_lists"][0]
    assert first["path"].endswith(".csv")
    assert first["parser"] == "csv"


def test_sources_upload_xlsx_rejects_corrupt_workbook(
    client: TestClient,
) -> None:
    """A malformed xlsx body must surface a clean 400 rather than
    crashing the endpoint."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": (
            "broken.xlsx", b"PK\x03\x04corrupt-not-a-real-zip",
            "application/octet-stream",
        )},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "xlsx" in detail["error"].lower()


# ---------- Phase 1 Supabase JWT auth -------------------------------------

_JWT_SECRET = "test-supabase-jwt-secret-do-not-use-anywhere-real"


def _make_jwt(
    *, sub: str = "11111111-1111-1111-1111-111111111111",
    secret: str = _JWT_SECRET, audience: str = "authenticated",
    ttl_seconds: int = 300, extra: dict | None = None,
) -> str:
    """Mint an HS256 JWT shaped like a Supabase user token. Default
    audience matches what `_verify_supabase_jwt` requires; tests pass
    `audience=...` to exercise the rejection path."""
    import time
    import jwt
    payload = {
        "sub": sub,
        "aud": audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
        "role": "authenticated",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def jwt_client(
    workspace_with_one_pending_draft: Path, monkeypatch,
) -> TestClient:
    """TestClient with SUPABASE_JWT_SECRET set so the JWT path is
    active. Fallback default-off so we can prove the JWT gate
    actually rejects non-JWT tokens unless explicitly allowed."""
    monkeypatch.setenv("API_KEY", "legacy-api-key")
    monkeypatch.setenv(
        "INVESTOR_WORKSPACE", str(workspace_with_one_pending_draft),
    )
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    # Default off; individual tests opt in via monkeypatch.setenv.
    monkeypatch.delenv("AUTH_ALLOW_API_KEY_FALLBACK", raising=False)
    import importlib
    import web.api as api_mod
    import web.deps as deps_mod
    importlib.reload(deps_mod)
    importlib.reload(api_mod)
    return TestClient(api_mod.app)


def test_jwt_valid_token_authenticates(jwt_client: TestClient) -> None:
    """Happy path: a JWT signed with the configured secret +
    `aud: authenticated` is accepted on a normally-authed endpoint."""
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": f"Bearer {_make_jwt()}"},
    )
    assert res.status_code == 200, res.text


def test_jwt_wrong_secret_rejected(jwt_client: TestClient) -> None:
    """A JWT signed with a different secret must NOT authenticate
    -- otherwise anyone with a Supabase project can issue tokens
    for our backend."""
    bad = _make_jwt(secret="some-other-secret")
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert res.status_code == 401
    assert "invalid token" in res.text.lower()


def test_jwt_expired_rejected(jwt_client: TestClient) -> None:
    """Expired tokens are refused (PyJWT default validates `exp`)."""
    expired = _make_jwt(ttl_seconds=-60)
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert res.status_code == 401


def test_jwt_wrong_audience_rejected(jwt_client: TestClient) -> None:
    """Non-authenticated audience (e.g. an anon token from a
    different aud claim) is refused so the gate stays narrow."""
    weird_aud = _make_jwt(audience="some-other-audience")
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": f"Bearer {weird_aud}"},
    )
    assert res.status_code == 401


def test_jwt_missing_sub_rejected(jwt_client: TestClient) -> None:
    """A token with no `sub` claim has no user_id to scope on.
    PyJWT raises via `options={"require": ["sub", "exp"]}`."""
    import time
    import jwt as pyjwt
    no_sub = pyjwt.encode(
        {"aud": "authenticated", "exp": int(time.time()) + 60},
        _JWT_SECRET, algorithm="HS256",
    )
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": f"Bearer {no_sub}"},
    )
    assert res.status_code == 401


def test_legacy_api_key_rejected_when_fallback_off(
    jwt_client: TestClient,
) -> None:
    """SUPABASE_JWT_SECRET set + fallback OFF -> the legacy shared
    key is refused. This is the post-cutover steady state the
    operator flips into once the frontend stops sending the key."""
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": "Bearer legacy-api-key"},
    )
    assert res.status_code == 401


def test_legacy_api_key_accepted_when_fallback_on(
    jwt_client: TestClient, monkeypatch,
) -> None:
    """Cutover window: both JWTs AND the legacy key work while
    AUTH_ALLOW_API_KEY_FALLBACK=true AND API_KEY_FALLBACK_USER_ID
    binds the shared key to a specific tenant. The frontend can
    ship JWT support without breaking already-running clients,
    and legacy traffic gets attributed to the configured uuid."""
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.setenv(
        "API_KEY_FALLBACK_USER_ID",
        "44444444-4444-4444-4444-444444444444",
    )
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": "Bearer legacy-api-key"},
    )
    assert res.status_code == 200, res.text


def test_legacy_api_key_rejected_when_fallback_on_but_no_uid_binding(
    jwt_client: TestClient, monkeypatch,
) -> None:
    """Phase 1.5 tightening: fallback ON but
    API_KEY_FALLBACK_USER_ID unset -> 401. Refusing is safer than
    silently mis-attributing legacy traffic to an unknown tenant."""
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.delenv("API_KEY_FALLBACK_USER_ID", raising=False)
    res = jwt_client.get(
        "/review/pending",
        headers={"Authorization": "Bearer legacy-api-key"},
    )
    assert res.status_code == 401
    assert "API_KEY_FALLBACK_USER_ID" in res.text


def test_legacy_api_key_still_works_when_jwt_secret_unset(
    client: TestClient,
) -> None:
    """Backwards-compat guarantee: a deployment that hasn't yet
    set SUPABASE_JWT_SECRET keeps working unchanged on the
    shared key. This test uses the original `client` fixture
    (no SUPABASE_JWT_SECRET in env)."""
    res = client.get("/review/pending", headers=_auth_headers())
    assert res.status_code == 200


def test_current_user_id_resolves_from_jwt_sub(
    jwt_client: TestClient,
) -> None:
    """Phase 2 prep: when the JWT path authenticates, the new
    `current_user_id` dependency returns the `sub` claim verbatim.
    Tests it via the dependency directly since no endpoint
    consumes it yet."""
    from fastapi import Request
    import web.deps as deps
    target_uuid = "22222222-2222-2222-2222-222222222222"
    token = _make_jwt(sub=target_uuid)
    # Call the dependency function directly with a synthesized
    # Authorization header value -- this is the same signature
    # FastAPI uses when resolving the dependency.
    uid = deps.current_user_id(authorization=f"Bearer {token}")
    assert uid == target_uuid


def test_current_user_id_resolves_from_env_on_api_key_path(
    monkeypatch,
) -> None:
    """When the legacy key authenticates, the user_id is sourced
    from API_KEY_FALLBACK_USER_ID -- typically pointed at the
    operator's admin uuid so pre-cutover traffic gets tagged to
    their tenant consistently.

    Phase 1.5 tightened this: the bearer token MUST match the
    configured API_KEY for the legacy path to authenticate.
    Sending an arbitrary string no longer works.
    """
    monkeypatch.setenv("API_KEY", "the-real-legacy-key")
    monkeypatch.setenv(
        "API_KEY_FALLBACK_USER_ID",
        "33333333-3333-3333-3333-333333333333",
    )
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    # Token must match the configured API_KEY -- legacy mode (no
    # JWT secret) treats this as the canonical auth.
    uid = deps.current_user_id(
        authorization="Bearer the-real-legacy-key",
    )
    assert uid == "33333333-3333-3333-3333-333333333333"


def test_current_user_id_is_none_when_no_auth() -> None:
    """No Authorization header at all -> None. The dependency does
    not raise -- it's informational. `require_auth` is the gate."""
    import web.deps as deps
    assert deps.current_user_id(authorization=None) is None
    assert deps.current_user_id(authorization="Basic foo") is None


# ---------- Phase 1.5 Principal extras (email + role) ---------------------

def test_current_principal_extracts_email_and_role_from_jwt(
    monkeypatch,
) -> None:
    """JWT path: email is the top-level claim Supabase sets, role
    comes from `app_metadata.role`. Both surface in the dict
    returned by `current_principal`."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    token = _make_jwt(
        sub="55555555-5555-5555-5555-555555555555",
        extra={
            "email": "ashley@kismet.fund",
            "app_metadata": {"role": "admin"},
        },
    )
    p = deps.current_principal(authorization=f"Bearer {token}")
    assert p == {
        "user_id": "55555555-5555-5555-5555-555555555555",
        "email": "ashley@kismet.fund",
        "role": "admin",
        "source": "jwt",
    }


def test_current_principal_role_fallback_to_top_level_claim(
    monkeypatch,
) -> None:
    """Supabase tokens carry `role: 'authenticated'` at the top
    level even when `app_metadata.role` is missing. The dependency
    surfaces that as the role until Phase 5's user_roles lookup
    upgrades it."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    # _make_jwt's default extra carries `role: "authenticated"`.
    token = _make_jwt()
    p = deps.current_principal(authorization=f"Bearer {token}")
    assert p is not None
    assert p["role"] == "authenticated"


def test_current_principal_api_key_path_resolves_to_admin(
    monkeypatch,
) -> None:
    """Spec: 'accept the old VITE_API_KEY as admin-equivalent
    bearer'. The legacy key path resolves to role='admin' so admin
    endpoints work during cutover without a Supabase admin row."""
    monkeypatch.setenv("API_KEY", "legacy-shared-key")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.setenv(
        "API_KEY_FALLBACK_USER_ID",
        "66666666-6666-6666-6666-666666666666",
    )
    monkeypatch.setenv("API_KEY_FALLBACK_EMAIL", "ops@kismet.fund")
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    p = deps.current_principal(authorization="Bearer legacy-shared-key")
    assert p == {
        "user_id": "66666666-6666-6666-6666-666666666666",
        "email": "ops@kismet.fund",
        "role": "admin",
        "source": "api_key",
    }


def test_current_principal_api_key_path_returns_none_when_uid_unset(
    monkeypatch,
) -> None:
    """Without API_KEY_FALLBACK_USER_ID, the legacy key path
    returns None from current_principal (and require_auth rejects
    the request entirely). This is the spec's 'reject rather than
    silently mis-attribute' guarantee at the dependency layer."""
    monkeypatch.setenv("API_KEY", "legacy-shared-key")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("AUTH_ALLOW_API_KEY_FALLBACK", "true")
    monkeypatch.delenv("API_KEY_FALLBACK_USER_ID", raising=False)
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    p = deps.current_principal(authorization="Bearer legacy-shared-key")
    assert p is None


def test_current_user_email_and_role_thin_wrappers(monkeypatch) -> None:
    """The dedicated -email / -role dependencies are thin shims
    around `current_principal`. Smoke them to confirm they return
    the same values."""
    monkeypatch.setenv("SUPABASE_JWT_SECRET", _JWT_SECRET)
    import importlib
    import web.deps as deps
    importlib.reload(deps)
    token = _make_jwt(extra={
        "email": "founder@example.com",
        "app_metadata": {"role": "moderator"},
    })
    auth = f"Bearer {token}"
    assert deps.current_user_email(authorization=auth) == "founder@example.com"
    assert deps.current_user_role(authorization=auth) == "moderator"
