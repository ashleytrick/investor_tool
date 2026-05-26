"""FastAPI backend for the external React frontend.

Mirrors the Streamlit operator UI's actions but exposed as JSON over
HTTPS so the `awesome_investor_tool` frontend (or any client with the
API key) can drive the pipeline from a browser. Like the Streamlit
UI, every mutating action shells out to the matching `scripts/*.py`
so the workspace lock + audit + backup story is unchanged.

Auth: a single shared API key passed as `Authorization: Bearer <key>`.
The key lives in the `API_KEY` env var; missing -> the server refuses
to start. CORS allow-list lives in `CORS_ORIGINS` (comma-separated);
default is `*` for local dev -- production should pin to the frontend's
exact origin.

Workspace is pinned via `INVESTOR_WORKSPACE` (same env var the
Streamlit UI uses). The API never lets a client pick a workspace at
runtime -- multi-tenant is a future concern.

Run locally:
    API_KEY=dev-key \\
    INVESTOR_WORKSPACE=clients/test_workspace \\
    uv run --extra api uvicorn web.api:app --reload --port 8080

OpenAPI spec is auto-generated at /openapi.json (and used by the
`web/openapi.json` dump script in CI for the frontend's type
generator).
"""
from __future__ import annotations

import hmac
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.approval.gate import (  # noqa: E402
    ApprovalGate,
    can_approve_draft,
    split_blockers,
)
from core.approval.persistence import (  # noqa: E402
    approved_for_send,
    pending_review,
)
from core import gmail_oauth  # noqa: E402
from core.config_loader import load_workspace  # noqa: E402
from core.db import (  # noqa: E402
    email_drafts,
    get_engine,
    partners,
    runs,
)


# ---------- pydantic response schemas ----------

class BlockerInfo(BaseModel):
    text: str
    severity: str  # "hard" or "soft"


class GateInfo(BaseModel):
    ok: bool
    blockers: list[BlockerInfo]
    overridden: list[str]


class DraftView(BaseModel):
    draft_id: int
    partner_id: str
    strategy: str | None = None
    subject: str | None = None
    body: str | None = None
    approval_status: str | None = None
    qa_status: str | None = None
    template_smell: str | None = None
    partner_email: str | None = None
    gate: GateInfo | None = None


class ApproveBody(BaseModel):
    notes: str = Field(min_length=1, description="Operator rationale; required for audit.")
    override_blockers: bool = False


class RejectBody(BaseModel):
    notes: str = Field(min_length=1)


class SetEmailBody(BaseModel):
    email: str = Field(min_length=3)


class CheckReadyResult(BaseModel):
    phase: str
    stdout: str
    blocked: bool
    return_code: int


class CommandResult(BaseModel):
    ok: bool
    stdout: str
    stderr: str = ""
    # Exit code of the wrapped script. Defaulted so existing
    # construction sites (approve / reject / set-email) don't need to
    # pass it explicitly -- those endpoints only reach the success
    # branch on returncode == 0 anyway.
    returncode: int = 0


# ---------- onboarding wizard schemas ----------

class ConfigInfo(BaseModel):
    """Snapshot the onboarding wizard polls. `mode` is the binary
    fixture/production view; the underlying `dry_run` value (a real
    workspace whose external syncs are gated off) surfaces as
    "production" since the wizard's question is "are you on fake data
    or your own data?", not "are external syncs armed?"."""
    mode: Literal["fixture", "production"]
    gmail_connected: bool
    # True when company.name + company.one_liner are both non-empty,
    # i.e. the operator finished Step 1 of the wizard.
    company_configured: bool


class CompanyProfile(BaseModel):
    """Step 1 onboarding form. Flat shape (the UI's contract).
    Only `name` and `one_liner` are required; everything else
    defaults to "" / None / [] so a half-filled form still round-trips
    cleanly through PUT -> GET.
    """
    # Identity.
    name: str = ""
    one_liner: str = ""
    website: str = ""
    founded_year: int | None = None
    hq_location: str = ""
    # Pitch.
    stage: str = ""
    sectors: list[str] = Field(default_factory=list)
    business_model: str = ""
    problem: str = ""
    solution: str = ""
    differentiators: str = ""
    why_now: str = ""
    traction: str = ""
    # Round.
    round_amount_usd: int | None = None
    round_instrument: str = ""
    round_valuation_usd: int | None = None
    round_close_target: str = ""
    # Investor fit.
    target_check_min_usd: int | None = None
    target_check_max_usd: int | None = None
    target_stages: list[str] = Field(default_factory=list)
    target_sectors: list[str] = Field(default_factory=list)
    target_geographies: list[str] = Field(default_factory=list)
    desired_traits: list[str] = Field(default_factory=list)
    # Anti-criteria.
    excluded_sectors: list[str] = Field(default_factory=list)
    excluded_geographies: list[str] = Field(default_factory=list)
    do_not_contact: list[str] = Field(default_factory=list)
    # Voice + outreach.
    founder_name: str = ""
    founder_title: str = ""
    founder_email: str = ""
    signature: str = ""
    tone: str = ""
    scheduling_link: str = ""


class SetModeBody(BaseModel):
    mode: Literal["fixture", "production"]


class GmailStatus(BaseModel):
    connected: bool


class GmailConnectResponse(BaseModel):
    auth_url: str


class RunRow(BaseModel):
    run_id: int
    stage: str | None
    started_at: str | None
    completed_at: str | None
    processed: int | None
    succeeded: int | None
    failed: int | None
    skipped: int | None
    error_summary: str | None


# ---------- helpers ----------

def _api_key() -> str:
    """Fail-fast on missing API_KEY at request time. We defer the
    check (rather than failing at import) so test clients can monkey
    the env var before each request."""
    key = os.environ.get("API_KEY")
    if not key:
        raise HTTPException(
            500,
            "server misconfigured: API_KEY env var is not set",
        )
    return key


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. Compares constant-time so the secret can't
    leak via timing. The frontend sends:
        Authorization: Bearer <API_KEY>
    """
    expected = _api_key()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "invalid api key")


def _ws_path() -> str:
    ws = os.environ.get("INVESTOR_WORKSPACE")
    if not ws:
        raise HTTPException(
            500,
            "server misconfigured: INVESTOR_WORKSPACE env var is not set",
        )
    return ws


def _engine_and_ws():
    """Load workspace + engine. Not cached -- engine creation is
    cheap; caching across requests risks stale config when files
    on disk change out-of-band (e.g. operator edits YAML)."""
    ws = load_workspace(_ws_path())
    return get_engine(ws.db_url), ws


def _actor() -> str:
    return os.environ.get("API_OPERATOR", "api-client")


def _run_cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / args[0]), *args[1:]]
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "USER": _actor()},
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


def _gate_to_dict(gate: ApprovalGate) -> GateInfo:
    hard, soft = split_blockers(gate.blockers)
    blockers: list[BlockerInfo] = []
    for b in hard:
        blockers.append(BlockerInfo(text=b, severity="hard"))
    for b in soft:
        blockers.append(BlockerInfo(text=b, severity="soft"))
    return GateInfo(
        ok=gate.ok, blockers=blockers, overridden=list(gate.overridden),
    )


def _serialize_draft(
    d: Any, *, partner_email: str | None, gate: GateInfo | None,
) -> DraftView:
    return DraftView(
        draft_id=int(d.draft_id),
        partner_id=str(d.partner_id),
        strategy=getattr(d, "strategy", None) or getattr(d, "email_strategy_used", None),
        subject=d.subject,
        body=d.body,
        approval_status=d.approval_status,
        qa_status=d.qa_status,
        template_smell=d.template_smell,
        partner_email=partner_email,
        gate=gate,
    )


# ---------- app ----------

app = FastAPI(
    title="Investor Outreach API",
    version="1.0.0",
    description=(
        "Backend for the external React frontend "
        "(`awesome_investor_tool`). All mutating endpoints shell "
        "out to the matching scripts/*.py so the existing workspace "
        "lock + audit + backup story is preserved."
    ),
)

# CORS allow-list. Two env vars, used together:
#
#   CORS_ORIGINS         comma-separated exact origins.
#   CORS_ORIGIN_REGEX    optional regex matched against the Origin
#                        header. Use when the frontend host generates
#                        ephemeral preview URLs (e.g. Lovable spawns
#                        a new `*--<project-id>.lovableproject.com`
#                        per session). Browser-sent origin = scheme
#                        + host only -- regex must NOT include path.
#
# starlette OR's the two: a request matches if its origin is in the
# explicit list OR matches the regex.
#
# Wildcard fallback ("*") fires ONLY when neither env var is set --
# typical local-dev shape. Production must pin one or both; a regex
# combined with an empty explicit list is the right pattern for a
# host like Lovable.
_origins_raw = os.environ.get("CORS_ORIGINS", "")
_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()]
_origin_regex = os.environ.get("CORS_ORIGIN_REGEX") or None
if not _origins and not _origin_regex:
    _origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/", include_in_schema=False)
def root():
    """Public health check (no auth) -- useful for uptime monitors."""
    return {
        "service": "investor-outreach-api",
        "status": "ok",
        "version": app.version,
    }


# ---------- review / approve ----------

@app.get(
    "/review/pending",
    response_model=list[DraftView],
    summary="Drafts pending operator review",
    tags=["review"],
)
def get_pending(_auth: None = Depends(require_auth)) -> list[DraftView]:
    engine, ws = _engine_and_ws()
    drafts = pending_review(engine)
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: r.email or ""
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    out: list[DraftView] = []
    for d in drafts:
        gate = can_approve_draft(
            ws, engine, int(d.draft_id), allow_example_domains=True,
        )
        out.append(_serialize_draft(
            d,
            partner_email=email_by_pid.get(d.partner_id),
            gate=_gate_to_dict(gate),
        ))
    return out


@app.get(
    "/drafts/approved",
    response_model=list[DraftView],
    summary="Drafts ready to send (approved live rows)",
    tags=["review"],
)
def get_approved(_auth: None = Depends(require_auth)) -> list[DraftView]:
    engine, _ = _engine_and_ws()
    drafts = approved_for_send(engine)
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: r.email or ""
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    return [
        _serialize_draft(
            d,
            partner_email=email_by_pid.get(d.partner_id),
            gate=None,  # gate is checked on approve; the queue is post-gate
        )
        for d in drafts
    ]


@app.post(
    "/drafts/{draft_id}/approve",
    response_model=CommandResult,
    summary="Approve a draft (shells out to approve_draft.py)",
    tags=["mutations"],
)
def approve_draft(
    draft_id: int, body: ApproveBody,
    _auth: None = Depends(require_auth),
) -> CommandResult:
    cli = [
        "approve_draft.py", "--workspace", _ws_path(),
        "--draft-id", str(draft_id),
        "--notes", body.notes,
        "--allow-example-domains",
    ]
    if body.override_blockers:
        cli.append("--override-blockers")
    res = _run_cli(*cli)
    if res.returncode != 0:
        raise HTTPException(
            400,
            detail={
                "error": "approve refused",
                "stdout": res.stdout,
                "stderr": res.stderr,
            },
        )
    return CommandResult(ok=True, stdout=res.stdout, stderr=res.stderr)


@app.post(
    "/drafts/{draft_id}/reject",
    response_model=CommandResult,
    summary="Reject a draft (shells out to reject_draft.py)",
    tags=["mutations"],
)
def reject_draft(
    draft_id: int, body: RejectBody,
    _auth: None = Depends(require_auth),
) -> CommandResult:
    res = _run_cli(
        "reject_draft.py", "--workspace", _ws_path(),
        "--draft-id", str(draft_id), "--notes", body.notes,
    )
    if res.returncode != 0:
        raise HTTPException(
            400,
            detail={
                "error": "reject failed",
                "stdout": res.stdout,
                "stderr": res.stderr,
            },
        )
    return CommandResult(ok=True, stdout=res.stdout, stderr=res.stderr)


# ---------- partner mutations ----------

@app.post(
    "/partners/{partner_id}/email",
    response_model=CommandResult,
    summary="Set a partner's email (shells out to set_partner_email.py)",
    tags=["mutations"],
)
def set_partner_email(
    partner_id: str, body: SetEmailBody,
    _auth: None = Depends(require_auth),
) -> CommandResult:
    res = _run_cli(
        "set_partner_email.py", "--workspace", _ws_path(),
        "--partner-id", partner_id, "--email", body.email,
    )
    if res.returncode != 0:
        raise HTTPException(
            400,
            detail={
                "error": "set_partner_email failed",
                "stdout": res.stdout,
                "stderr": res.stderr,
            },
        )
    return CommandResult(ok=True, stdout=res.stdout, stderr=res.stderr)


# ---------- gates / status ----------

@app.get(
    "/check_ready",
    response_model=CheckReadyResult,
    summary="Pre-flight check (review|send|gmail|attio)",
    tags=["status"],
)
def check_ready(
    phase: str = Query(
        default="send",
        pattern="^(review|send|gmail|attio)$",
        description="Which workflow phase to gate on.",
    ),
    _auth: None = Depends(require_auth),
) -> CheckReadyResult:
    res = _run_cli(
        "check_ready.py", "--workspace", _ws_path(),
        "--for", phase, "--allow-example-domains",
    )
    return CheckReadyResult(
        phase=phase,
        stdout=res.stdout,
        blocked="BLOCKED" in res.stdout,
        return_code=res.returncode,
    )


@app.get(
    "/runs",
    response_model=list[RunRow],
    summary="Recent run entries",
    tags=["status"],
)
def get_runs(
    limit: int = Query(50, ge=1, le=500),
    _auth: None = Depends(require_auth),
) -> list[RunRow]:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                runs.c.run_id, runs.c.stage, runs.c.started_at,
                runs.c.completed_at,
                runs.c.records_processed, runs.c.records_succeeded,
                runs.c.records_failed, runs.c.records_skipped,
                runs.c.error_summary,
            ).order_by(desc(runs.c.run_id)).limit(limit)
        ))
    return [
        RunRow(
            run_id=int(r.run_id),
            stage=r.stage,
            started_at=str(r.started_at) if r.started_at else None,
            completed_at=str(r.completed_at) if r.completed_at else None,
            processed=r.records_processed,
            succeeded=r.records_succeeded,
            failed=r.records_failed,
            skipped=r.records_skipped,
            error_summary=r.error_summary,
        )
        for r in rows
    ]


# ---------- export ----------

@app.get(
    "/send_queue.csv",
    summary="Build + download the send_queue CSV",
    tags=["export"],
    responses={200: {"content": {"text/csv": {}}}},
)
def send_queue_csv(_auth: None = Depends(require_auth)) -> FileResponse:
    """Calls export_send_queue.py to materialize the CSV, then
    streams the file. Returns 400 if the export refuses (e.g. no
    approved drafts; stale approvals not skipped)."""
    res = _run_cli(
        "export_send_queue.py", "--workspace", _ws_path(),
        "--allow-example-domains",
    )
    if res.returncode != 0:
        raise HTTPException(
            400,
            detail={
                "error": "export refused",
                "stdout": res.stdout,
                "stderr": res.stderr,
            },
        )
    _, ws = _engine_and_ws()
    csv_path = pathlib.Path(ws.path) / "exports" / "send_queue.csv"
    if not csv_path.exists():
        raise HTTPException(
            500, f"export reported success but file is missing: {csv_path}",
        )
    return FileResponse(
        path=str(csv_path),
        media_type="text/csv",
        filename="send_queue.csv",
    )


# ---------- onboarding wizard ----------
#
# These 6 endpoints back the React frontend's /onboarding route. The
# wizard walks an operator from "fresh checkout" to "drafts ready for
# review" by flipping company.yaml out of fixture mode, running stages
# 6 + 7, and linking Gmail -- four things the dashboard could not do
# without shelling out / editing files on the API host. Conventions
# match the existing routes: Bearer auth on every endpoint (except the
# OAuth callback, see below), subprocess failures surface as HTTP 400
# with detail={error, stdout, stderr, returncode}.

_MODE_LINE = re.compile(r"^(mode:\s*)(\S+)(.*)$", re.MULTILINE)
# A top-level YAML key at column 0: alnum/underscore identifier
# followed by `:`. Used to find the start + end of the `company:`
# block when rewriting it in place.
_TOPLEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _load_yaml_safely(path: pathlib.Path) -> dict:
    """Parse YAML from disk, tolerating missing / empty files."""
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard dep
        raise HTTPException(500, f"PyYAML missing: {exc}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - propagate parse errors
        raise HTTPException(
            500, f"company.yaml is not valid YAML: {exc}",
        )
    return data or {}


def _read_company_block(yaml_path: pathlib.Path) -> "CompanyProfile":
    """Build a CompanyProfile from disk.

    Reads the flat fields directly from `company:`, falling back to
    the legacy nested keys (`target_check_size_usd.{min,max}`,
    `current_traction.headline_metric`,
    `meeting_ask.preferred_scheduling_link`) when the flat counterpart
    is empty. This means a workspace that's only ever been edited
    through the CLI (test_workspace) surfaces sensible values to the
    UI without first being re-written via PUT.
    """
    data = _load_yaml_safely(yaml_path)
    c = (data.get("company") or {}) if isinstance(data, dict) else {}

    def _str(k: str, default: str = "") -> str:
        v = c.get(k)
        return v if isinstance(v, str) else default

    def _list(k: str) -> list[str]:
        v = c.get(k)
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    def _int(k: str) -> int | None:
        v = c.get(k)
        if isinstance(v, bool):  # bool is an int subclass; reject it
            return None
        if isinstance(v, int):
            return v
        return None

    # Legacy nested fallbacks.
    nested_check = c.get("target_check_size_usd") or {}
    legacy_check_min = (
        nested_check.get("min") if isinstance(nested_check, dict) else None
    )
    legacy_check_max = (
        nested_check.get("max") if isinstance(nested_check, dict) else None
    )
    legacy_traction = ""
    ct = c.get("current_traction")
    if isinstance(ct, dict):
        legacy_traction = ct.get("headline_metric") or ""
    legacy_scheduling = ""
    ma = c.get("meeting_ask")
    if isinstance(ma, dict):
        legacy_scheduling = ma.get("preferred_scheduling_link") or ""

    return CompanyProfile(
        name=_str("name"),
        one_liner=_str("one_liner"),
        website=_str("website"),
        founded_year=_int("founded_year"),
        hq_location=_str("hq_location"),
        stage=_str("stage"),
        sectors=_list("sectors"),
        business_model=_str("business_model"),
        problem=_str("problem"),
        solution=_str("solution"),
        differentiators=_str("differentiators"),
        why_now=_str("why_now"),
        traction=_str("traction") or legacy_traction,
        round_amount_usd=_int("round_amount_usd"),
        round_instrument=_str("round_instrument"),
        round_valuation_usd=_int("round_valuation_usd"),
        round_close_target=_str("round_close_target"),
        target_check_min_usd=(
            _int("target_check_min_usd")
            or (int(legacy_check_min) if isinstance(legacy_check_min, int) else None)
        ),
        target_check_max_usd=(
            _int("target_check_max_usd")
            or (int(legacy_check_max) if isinstance(legacy_check_max, int) else None)
        ),
        target_stages=_list("target_stages"),
        target_sectors=_list("target_sectors"),
        target_geographies=_list("target_geographies"),
        desired_traits=_list("desired_traits"),
        excluded_sectors=_list("excluded_sectors"),
        excluded_geographies=_list("excluded_geographies"),
        do_not_contact=_list("do_not_contact"),
        founder_name=_str("founder_name"),
        founder_title=_str("founder_title"),
        founder_email=_str("founder_email"),
        signature=_str("signature"),
        tone=_str("tone"),
        scheduling_link=_str("scheduling_link") or legacy_scheduling,
    )


def _company_dict_for_yaml(profile: "CompanyProfile") -> dict:
    """Render a CompanyProfile as the dict to dump under `company:`.

    Stores every flat field the UI sent, AND mirrors the three legacy
    nested keys the existing pipeline code still reads
    (core/round_fit.py, core/email/prompt.py, core/email/draft_routing.py).
    The mirror keeps stages 6/7 working unchanged on a workspace whose
    `company:` block was last written by this endpoint.
    """
    out: dict = {
        "name": profile.name,
        "one_liner": profile.one_liner,
        "website": profile.website,
        "founded_year": profile.founded_year,
        "hq_location": profile.hq_location,
        "stage": profile.stage,
        "sectors": list(profile.sectors),
        "business_model": profile.business_model,
        "problem": profile.problem,
        "solution": profile.solution,
        "differentiators": profile.differentiators,
        "why_now": profile.why_now,
        "traction": profile.traction,
        "round_amount_usd": profile.round_amount_usd,
        "round_instrument": profile.round_instrument,
        "round_valuation_usd": profile.round_valuation_usd,
        "round_close_target": profile.round_close_target,
        "target_check_min_usd": profile.target_check_min_usd,
        "target_check_max_usd": profile.target_check_max_usd,
        "target_stages": list(profile.target_stages),
        "target_sectors": list(profile.target_sectors),
        "target_geographies": list(profile.target_geographies),
        "desired_traits": list(profile.desired_traits),
        "excluded_sectors": list(profile.excluded_sectors),
        "excluded_geographies": list(profile.excluded_geographies),
        "do_not_contact": list(profile.do_not_contact),
        "founder_name": profile.founder_name,
        "founder_title": profile.founder_title,
        "founder_email": profile.founder_email,
        "signature": profile.signature,
        "tone": profile.tone,
        "scheduling_link": profile.scheduling_link,
    }
    # Legacy mirrors for the existing pipeline code paths.
    if profile.target_check_min_usd is not None or profile.target_check_max_usd is not None:
        out["target_check_size_usd"] = {
            "min": profile.target_check_min_usd or 0,
            "max": profile.target_check_max_usd or 0,
        }
    if profile.traction:
        out["current_traction"] = {"headline_metric": profile.traction}
    if profile.scheduling_link:
        out["meeting_ask"] = {
            "preferred_scheduling_link": profile.scheduling_link,
        }
    return out


def _write_company_block(
    yaml_path: pathlib.Path, profile: "CompanyProfile",
) -> None:
    """Rewrite (or append) the top-level `company:` block in place.

    Algorithm: find the line range spanned by `company:` (start =
    line that begins with `company:`, end = first top-level key
    that follows), replace it with a fresh YAML dump of just that
    one block, leave every byte outside the range untouched. The
    result preserves: the `mode:` line, sibling blocks
    (`raise_context`, `founder_voice`, `round_fit`, ...), and every
    comment that doesn't live inside the `company:` block.

    Comments INSIDE the company block are dropped (PyYAML doesn't
    round-trip them; ruamel.yaml would, but it's not a runtime dep).
    The brief mentions this is acceptable -- the company block is
    operator-mutable through the UI and rarely hand-edited.
    """
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(500, f"PyYAML missing: {exc}")

    company_dict = _company_dict_for_yaml(profile)
    rendered = yaml.safe_dump(
        {"company": company_dict},
        sort_keys=False, allow_unicode=True, default_flow_style=False,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"

    if not yaml_path.exists() or not yaml_path.read_text(encoding="utf-8").strip():
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(rendered, encoding="utf-8")
        return

    original = yaml_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    start: int | None = None
    end: int | None = None
    for i, line in enumerate(lines):
        m = _TOPLEVEL_KEY.match(line)
        if not m:
            continue
        if start is None:
            if m.group(1) == "company":
                start = i
            continue
        # Already inside the company block; first top-level key after
        # start closes the range.
        end = i
        break
    if start is None:
        # Append at the end with a blank-line separator so the new
        # block is visually distinct from whatever came before.
        sep = "" if original.endswith("\n\n") or not original else "\n"
        yaml_path.write_text(original + sep + rendered, encoding="utf-8")
        return
    if end is None:
        end = len(lines)
    new_text = "".join(lines[:start]) + rendered + "".join(lines[end:])
    yaml_path.write_text(new_text, encoding="utf-8")


def _read_mode_from_yaml(yaml_path: pathlib.Path) -> str:
    """Wizard-facing mode value. The on-disk vocabulary is
    {fixture, dry_run, production} (see core/config_loader.py); the
    wizard only cares about fixture vs not-fixture, so dry_run maps to
    production here."""
    if not yaml_path.exists():
        raise HTTPException(500, f"company.yaml missing at {yaml_path}")
    text = yaml_path.read_text(encoding="utf-8")
    m = _MODE_LINE.search(text)
    if not m:
        # config_loader defaults absence to dry_run; surface as production.
        return "production"
    value = m.group(2).strip().strip("\"'")
    return "fixture" if value == "fixture" else "production"


def _write_mode_to_yaml(yaml_path: pathlib.Path, new_mode: str) -> None:
    """Replace (or insert) the top-level `mode:` line in company.yaml
    in place, preserving comments and surrounding formatting. A regex
    edit avoids re-rendering the whole document the way PyYAML's
    round-trip would (PyYAML drops comments; ruamel.yaml is not in our
    deps). Only one short line ever changes, so a string-level edit is
    sufficient."""
    if not yaml_path.exists():
        raise HTTPException(500, f"company.yaml missing at {yaml_path}")
    text = yaml_path.read_text(encoding="utf-8")
    if _MODE_LINE.search(text):
        new_text = _MODE_LINE.sub(
            lambda m: f"{m.group(1)}{new_mode}{m.group(3)}",
            text, count=1,
        )
    else:
        # Insert after the leading run of comment / blank lines so the
        # mode declaration sits above the actual config block.
        lines = text.splitlines()
        insert_at = 0
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped and not stripped.startswith("#"):
                insert_at = i
                break
        new_line = f"mode: {new_mode}"
        # Pad with a blank line on either side so the inserted line
        # reads as its own block instead of glomming onto the next key.
        to_insert = [new_line, ""]
        if insert_at > 0 and lines[insert_at - 1].strip() != "":
            to_insert = ["", *to_insert]
        for offset, line in enumerate(to_insert):
            lines.insert(insert_at + offset, line)
        new_text = "\n".join(lines)
        if text.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
    yaml_path.write_text(new_text, encoding="utf-8")


@app.get(
    "/config",
    response_model=ConfigInfo,
    summary="Onboarding wizard config snapshot",
    tags=["onboarding"],
)
def get_config(_auth: None = Depends(require_auth)) -> ConfigInfo:
    _, ws = _engine_and_ws()
    yaml_path = ws.config_dir / "company.yaml"
    profile = _read_company_block(yaml_path)
    return ConfigInfo(
        mode=_read_mode_from_yaml(yaml_path),
        gmail_connected=gmail_oauth.is_connected(ws),
        company_configured=bool(profile.name) and bool(profile.one_liner),
    )


@app.post(
    "/config/mode",
    response_model=CommandResult,
    summary="Flip company.yaml `mode:` (fixture <-> production)",
    tags=["onboarding"],
)
def set_mode(
    body: SetModeBody, _auth: None = Depends(require_auth),
) -> CommandResult:
    _, ws = _engine_and_ws()
    try:
        _write_mode_to_yaml(ws.config_dir / "company.yaml", body.mode)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - report cleanly to the frontend
        raise HTTPException(
            400,
            detail={
                "error": "failed to update company.yaml",
                "stdout": "",
                "stderr": str(exc),
                "returncode": 1,
            },
        )
    return CommandResult(
        ok=True, returncode=0,
        stdout=f"company.yaml mode set to {body.mode}\n",
        stderr="",
    )


@app.get(
    "/config/company",
    response_model=CompanyProfile,
    summary="Read the company profile (Step 1 form values)",
    tags=["onboarding"],
)
def get_company(_auth: None = Depends(require_auth)) -> CompanyProfile:
    """Returns the flat company profile. Never 404s on missing file --
    a fresh workspace gets the empty shape so the form's controlled
    inputs always have a defined value."""
    _, ws = _engine_and_ws()
    return _read_company_block(ws.config_dir / "company.yaml")


@app.put(
    "/config/company",
    response_model=CommandResult,
    summary="Write the company profile to company.yaml",
    tags=["onboarding"],
)
def put_company(
    body: CompanyProfile, _auth: None = Depends(require_auth),
) -> CommandResult:
    _, ws = _engine_and_ws()
    yaml_path = ws.config_dir / "company.yaml"
    try:
        _write_company_block(yaml_path, body)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - report cleanly to the frontend
        raise HTTPException(
            400,
            detail={
                "error": "failed to update company.yaml",
                "stdout": "",
                "stderr": str(exc),
                "returncode": 1,
            },
        )
    return CommandResult(
        ok=True, returncode=0,
        stdout=f"company.yaml `company:` block updated ({body.name or 'unnamed'})\n",
        stderr="",
    )


def _shell_pipeline_stage(
    script: str, *extra_args: str, label: str,
) -> CommandResult:
    """Shared wrapper for /pipeline/* endpoints. Uses a 10-min timeout
    because Stage 6 (LLM-scored axes) and Stage 7 (LLM-drafted emails)
    both fan out per partner and the request must outlast the slowest
    fixture run."""
    res = _run_cli(
        script, "--workspace", _ws_path(), *extra_args, timeout=600,
    )
    if res.returncode != 0:
        raise HTTPException(
            400,
            detail={
                "error": f"{label} failed",
                "stdout": res.stdout,
                "stderr": res.stderr,
                "returncode": res.returncode,
            },
        )
    return CommandResult(
        ok=True, returncode=res.returncode,
        stdout=res.stdout, stderr=res.stderr,
    )


@app.post(
    "/pipeline/score",
    response_model=CommandResult,
    summary="Run Stage 6 (score_candidates) for the wizard",
    tags=["onboarding"],
)
def pipeline_score(_auth: None = Depends(require_auth)) -> CommandResult:
    return _shell_pipeline_stage(
        "06_score_candidates.py", label="score_candidates",
    )


@app.post(
    "/pipeline/generate",
    response_model=CommandResult,
    summary="Run Stage 7 (generate_emails) for the wizard",
    tags=["onboarding"],
)
def pipeline_generate(_auth: None = Depends(require_auth)) -> CommandResult:
    # Cap at TOP_BEFORE_CALIBRATION_REQUIRED (=10 in scripts/07) so the
    # wizard's run never trips Gate 5.5's calibration refusal. Real
    # operators scale higher via the CLI after the calibration cohort
    # comes back Green. --allow-example-domains is a no-op for real
    # workspaces and lets the fixture path through.
    return _shell_pipeline_stage(
        "07_generate_emails.py",
        "--top", "10",
        "--allow-example-domains",
        label="generate_emails",
    )


@app.get(
    "/gmail/status",
    response_model=GmailStatus,
    summary="Is Gmail OAuth completed for the pinned workspace?",
    tags=["onboarding"],
)
def gmail_status(_auth: None = Depends(require_auth)) -> GmailStatus:
    _, ws = _engine_and_ws()
    return GmailStatus(connected=gmail_oauth.is_connected(ws))


@app.post(
    "/gmail/connect",
    response_model=GmailConnectResponse,
    summary="Start Gmail OAuth; returns Google's auth URL",
    tags=["onboarding"],
)
def gmail_connect(
    request: Request, _auth: None = Depends(require_auth),
) -> GmailConnectResponse:
    _, ws = _engine_and_ws()
    redirect_uri = str(request.url_for("gmail_oauth_callback"))
    try:
        auth_url, _state = gmail_oauth.start_flow(ws, redirect_uri)
    except FileNotFoundError as exc:
        raise HTTPException(
            400,
            detail={
                "error": str(exc),
                "stdout": "",
                "stderr": "",
                "returncode": 1,
            },
        )
    return GmailConnectResponse(auth_url=auth_url)


@app.get(
    "/oauth/gmail/callback",
    include_in_schema=False,
    name="gmail_oauth_callback",
)
def gmail_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Google redirects the operator's browser here after consent.

    Auth model: NO Bearer header (browsers can't attach custom headers
    to a cross-origin redirect from accounts.google.com). The `state`
    parameter -- minted server-side inside an authenticated
    /gmail/connect call -- works as a single-use bearer because it's
    cryptographically random and we delete it on first use. This is the
    standard OAuth CSRF / auth pattern, not a missing auth check.
    """
    if error:
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<title>Gmail OAuth failed</title>"
            f"<h1>Gmail OAuth failed</h1><pre>{error}</pre>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<title>Gmail OAuth error</title>"
            "<h1>Missing code or state on OAuth redirect</h1>",
            status_code=400,
        )
    _, ws = _engine_and_ws()
    try:
        profile = gmail_oauth.complete_flow(state, code, ws)
    except ValueError as exc:
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<title>Gmail OAuth error</title>"
            f"<h1>OAuth callback rejected</h1><pre>{exc}</pre>",
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001 - Google SDK throws diverse types
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<title>Gmail OAuth error</title>"
            f"<h1>Token exchange failed</h1><pre>{exc}</pre>",
            status_code=400,
        )
    email = profile.get("emailAddress", "(unknown)")
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'>"
        f"<title>Gmail linked</title>"
        f"<h1>Gmail linked</h1>"
        f"<p>Connected as <b>{email}</b>. "
        f"You can close this tab and return to the dashboard.</p>"
    )
