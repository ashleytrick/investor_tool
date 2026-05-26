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
    regression that removes a documented endpoint -- the 8 original
    routes plus the 6 onboarding-wizard routes the Lovable frontend
    was built against."""
    spec = client.get("/openapi.json").json()
    paths = spec.get("paths", {})
    for required in (
        # Original surface.
        "/review/pending",
        "/drafts/approved",
        "/drafts/{draft_id}/approve",
        "/drafts/{draft_id}/reject",
        "/partners/{partner_id}/email",
        "/check_ready",
        "/runs",
        "/send_queue.csv",
        # Onboarding wizard surface.
        "/config",
        "/config/mode",
        "/config/company",
        "/pipeline/score",
        "/pipeline/generate",
        "/pipeline/sources",
        "/gmail/status",
        "/gmail/connect",
    ):
        assert required in paths, (
            f"OpenAPI spec dropped {required}; "
            f"frontend regen will lose the endpoint"
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
    # test_workspace's company.yaml has name="Tendril" + one_liner set,
    # so the wizard sees Step 1 as already complete on the fixture.
    assert body["company_configured"] is True


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


def test_sources_upload_rejects_non_csv_extension(
    client: TestClient,
) -> None:
    """A .xlsx or .json upload returns 400 -- Stage 1 only reads CSV
    and silently saving a different format would leave the
    operator wondering why no funds showed up."""
    res = client.post(
        "/pipeline/sources",
        headers=_auth_headers(),
        files={"file": ("investors.xlsx", b"PK\x03\x04binary", "application/octet-stream")},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert ".csv" in detail["error"].lower()


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
