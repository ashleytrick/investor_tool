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
import subprocess
import sys
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

# CORS: comma-separated origins via env, default "*" for local dev.
# Production should set CORS_ORIGINS to the exact frontend origin.
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
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
