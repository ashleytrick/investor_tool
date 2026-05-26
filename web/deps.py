"""Shared FastAPI dependencies + helpers.

Hoisted out of `web/api.py` so the per-feature routers under
`web/routers/` can import them without creating a circular import
back to the main app module. The behavior is unchanged -- this file
is purely a relocation.
"""
from __future__ import annotations

import hmac
import os
import pathlib
import subprocess
import sys

from fastapi import Depends, HTTPException, Header

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config_loader import load_workspace  # noqa: E402
from core.db import get_engine, partner_score_summaries  # noqa: E402
from sqlalchemy import select as _select  # noqa: E402


# ---------- shared response shapes ----------
#
# Hoisted here (refactor #16) so per-feature routers under
# web/routers/ can return them without importing back into
# web/api.py and creating circular imports. DraftView in
# particular is shared by /review/pending (still in api.py)
# and /today (coach router).

from pydantic import BaseModel as _BaseModel  # noqa: E402


class BlockerInfo(_BaseModel):
    text: str
    severity: str  # "hard" or "soft"


class GateInfo(_BaseModel):
    ok: bool
    blockers: list[BlockerInfo]
    overridden: list[str]


class DraftView(_BaseModel):
    """Operator-facing view of a draft. B1 added the `rationale`
    field, sourced from `partner_score_summaries.recommendation_reasoning`."""
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
    rationale: str | None = None


class CommandResult(_BaseModel):
    ok: bool
    stdout: str
    stderr: str = ""
    returncode: int = 0


def gate_to_dict(gate) -> GateInfo:
    """Convert an `ApprovalGate` to a GateInfo response model.
    Lazy import so this module doesn't pull approval-gate
    machinery at boot."""
    from core.approval.gate import split_blockers
    hard, soft = split_blockers(gate.blockers)
    blockers: list[BlockerInfo] = []
    for b in hard:
        blockers.append(BlockerInfo(text=b, severity="hard"))
    for b in soft:
        blockers.append(BlockerInfo(text=b, severity="soft"))
    return GateInfo(
        ok=gate.ok, blockers=blockers, overridden=list(gate.overridden),
    )


def serialize_draft(
    d, *,
    partner_email: str | None,
    gate: GateInfo | None,
    rationale: str | None = None,
) -> DraftView:
    """Project an `email_drafts` row into a DraftView. Shared
    between coach router (/today) and api.py (/review/pending,
    /drafts/approved)."""
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
        rationale=rationale,
    )


def rationale_by_partner(conn) -> dict[str, str]:
    """Map partner_id -> Stage 6 recommendation_reasoning.

    Returns an empty dict for workspaces that have never run Stage 6
    (the table exists from `metadata.create_all` but is empty); the
    caller treats missing entries as `rationale=None` rather than
    as an error.
    """
    rows = conn.execute(
        _select(
            partner_score_summaries.c.partner_id,
            partner_score_summaries.c.recommendation_reasoning,
        )
    )
    return {
        r.partner_id: r.recommendation_reasoning
        for r in rows
        if r.recommendation_reasoning
    }


# ---------- request-time env checks ----------

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


def _jwt_secret() -> str | None:
    """Supabase JWT signing secret.

    When unset, the JWT path is disabled and `require_auth` falls
    back to the legacy shared `API_KEY` unconditionally (so existing
    deployments don't break on the day this code lands). Operators
    enabling Supabase auth set `SUPABASE_JWT_SECRET` to the value
    from Supabase dashboard -> Settings -> API -> JWT Secret.
    """
    return os.environ.get("SUPABASE_JWT_SECRET") or None


def _is_api_key_fallback_enabled() -> bool:
    """Should the legacy shared-API_KEY token still be accepted?

    Cutover affordance: the frontend is switching from
    `VITE_API_KEY` to per-user Supabase JWTs. During the cutover
    window both paths must work; the operator flips this off once
    the frontend is fully on JWTs.
    """
    raw = os.environ.get("AUTH_ALLOW_API_KEY_FALLBACK", "")
    return raw.lower() in {"1", "true", "yes", "on"}


def _verify_supabase_jwt(token: str) -> dict | None:
    """Verify a Supabase HS256 JWT. Returns the claims dict on
    success, None on any failure (expired, wrong signature, missing
    `sub`, wrong audience, etc.).

    Failure modes are deliberately collapsed -- the caller decides
    whether to fall back or refuse. Supabase user tokens carry
    `aud: "authenticated"`; service-role keys carry the same
    audience but `role: "service_role"`. We accept any token that
    verifies + has a `sub`, and let role-based gating (e.g. admin
    endpoints) read `role` / `user_roles.role` downstream.
    """
    secret = _jwt_secret()
    if not secret:
        return None
    try:
        import jwt  # PyJWT
    except ImportError:
        return None
    try:
        claims = jwt.decode(
            token, secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["sub", "exp"]},
        )
    except Exception:  # noqa: BLE001 - PyJWT raises diverse subclasses
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    return claims


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. Accepts either:

      1. A Supabase HS256 JWT signed with `SUPABASE_JWT_SECRET`
         (preferred), or
      2. The legacy shared `API_KEY` (fallback during cutover).

    Default behavior:
      - SUPABASE_JWT_SECRET unset -> JWT path is disabled; the
        API_KEY fallback is always on (legacy mode; nothing breaks
        for deployments that haven't yet wired Supabase).
      - SUPABASE_JWT_SECRET set -> JWT path is preferred; the
        API_KEY fallback runs only when
        `AUTH_ALLOW_API_KEY_FALLBACK=true`.

    The frontend's eventual end state is JWT-only; flip the
    fallback off once it stops sending the legacy key.

    Side effect (post-#1-review): on a successful auth this also
    sets the `_CURRENT_USER_ID_VAR` contextvar so `_engine_and_ws()`
    and `_ws_path()` route per-tenant even when the endpoint only
    declared `Depends(require_auth)` (not `current_principal`).
    Previously only `current_principal` set the var, which meant
    most authed routes silently fell back to the pinned workspace.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    jwt_mode = _jwt_secret() is not None

    # 1) Supabase JWT path (when configured).
    if jwt_mode:
        claims = _verify_supabase_jwt(token)
        if claims is not None:
            principal = _principal_from_claims(claims)
            if principal and principal.get("user_id"):
                _CURRENT_USER_ID_VAR.set(principal["user_id"])
            return

    # 2) Legacy API_KEY fallback.
    # - In legacy mode (no JWT secret), this is always on -- existing
    #   pre-cutover deployments keep working unchanged.
    # - In JWT mode, only when AUTH_ALLOW_API_KEY_FALLBACK is set
    #   AND `API_KEY_FALLBACK_USER_ID` binds the shared key to a
    #   specific tenant. Without the binding env var, we'd be
    #   silently mis-attributing legacy traffic to an unknown
    #   tenant; per the spec we refuse instead.
    fallback_on = (not jwt_mode) or _is_api_key_fallback_enabled()
    if fallback_on:
        expected = os.environ.get("API_KEY") or ""
        if expected and hmac.compare_digest(token, expected):
            if jwt_mode and not (
                os.environ.get("API_KEY_FALLBACK_USER_ID") or ""
            ):
                raise HTTPException(
                    401,
                    "API_KEY accepted but API_KEY_FALLBACK_USER_ID "
                    "is unset; refusing to attribute legacy traffic "
                    "to an unknown tenant",
                )
            # Stamp the contextvar from the bound UUID env so
            # downstream routing follows the same tenant as the
            # JWT path would have.
            fallback_uid = os.environ.get("API_KEY_FALLBACK_USER_ID")
            if fallback_uid:
                _CURRENT_USER_ID_VAR.set(fallback_uid)
            return

    raise HTTPException(
        401,
        "invalid token" if jwt_mode else "invalid api key",
    )


def _api_key_fallback_principal() -> dict | None:
    """Build the Principal-shaped dict returned to callers when the
    legacy API_KEY path authenticates.

    The legacy key is shared (not per-user), so we attribute its
    traffic to a specific UUID via `API_KEY_FALLBACK_USER_ID` env
    var (typically the operator's own admin uuid). Without that
    env var, we have no honest user_id to scope queries by --
    returning a principal with `user_id=None` would invite silent
    cross-tenant reads, so we return None here and let
    `require_auth` reject the request with 401.

    Spec: 'For backward-compat during migration, also accept the
    old VITE_API_KEY as admin-equivalent bearer.' -> role='admin'
    so admin endpoints work during the cutover window without
    needing an actual Supabase admin row.
    """
    uid = os.environ.get("API_KEY_FALLBACK_USER_ID") or ""
    if not uid:
        return None
    return {
        "user_id": uid,
        "email": os.environ.get("API_KEY_FALLBACK_EMAIL") or None,
        "role": "admin",
        "source": "api_key",
    }


def _principal_from_claims(claims: dict) -> dict:
    """Project Supabase JWT claims into the Principal shape callers
    consume. Pulls `email` directly from the top-level claim
    Supabase sets, and `role` from `app_metadata.role` (Supabase
    convention) with a fallback to the JWT's top-level `role`
    (which Supabase uses for the broad `authenticated` /
    `service_role` tag, not the app's role enum).

    The per-tenant Supabase `public.user_roles.role` lookup that
    upgrades 'authenticated' -> 'admin' / 'moderator' belongs to
    Phase 5; this Phase 1.5 function only surfaces what's already
    in the JWT.
    """
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return {}  # caller treats empty as not-authenticated
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    app_meta = claims.get("app_metadata") or {}
    role = None
    if isinstance(app_meta, dict):
        candidate = app_meta.get("role")
        if isinstance(candidate, str) and candidate:
            role = candidate
    if role is None:
        candidate = claims.get("role")
        if isinstance(candidate, str) and candidate:
            role = candidate
    return {
        "user_id": sub,
        "email": email,
        "role": role,
        "source": "jwt",
    }


def current_principal(
    authorization: str | None = Header(default=None),
) -> dict | None:
    """Return the authenticated principal as a dict:

      {"user_id": <uuid>, "email": <email|None>,
       "role": <role|None>, "source": "jwt" | "api_key"}

    Source order:
      1. Supabase JWT (claims via `_verify_supabase_jwt`)
      2. Legacy API_KEY path -- only if API_KEY matches AND
         `API_KEY_FALLBACK_USER_ID` is set. Without the env var,
         legacy-key requests are NOT honored by this dependency
         (and `require_auth` rejects them with 401 below).
      3. None when neither path resolves.

    Side effect: on a successful resolution, sets the request-scoped
    `_CURRENT_USER_ID_VAR` contextvar so `_engine_and_ws()` routes to
    the per-user workspace without each endpoint having to thread
    `user_id` through. The contextvar is local to the request task,
    so concurrent requests stay isolated.

    Pure read otherwise; does NOT raise. `require_auth` is the gate.
    """
    principal: dict | None = None
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()

    if _jwt_secret() is not None:
        claims = _verify_supabase_jwt(token)
        if claims is not None:
            candidate = _principal_from_claims(claims)
            if candidate:
                principal = candidate

    if principal is None:
        # API_KEY path -- only honored when the env var binds it to
        # a specific user_id.
        fallback_on = (
            _jwt_secret() is None or _is_api_key_fallback_enabled()
        )
        if fallback_on:
            expected = os.environ.get("API_KEY") or ""
            if expected and hmac.compare_digest(token, expected):
                principal = _api_key_fallback_principal()

    if principal is not None:
        _CURRENT_USER_ID_VAR.set(principal["user_id"])
    return principal


def current_user_id(
    authorization: str | None = Header(default=None),
) -> str | None:
    """Compatibility shim around `current_principal`. Returns just
    the user_id string (or None). Endpoints that need email / role
    use `current_principal` directly.
    """
    p = current_principal(authorization=authorization)
    return p.get("user_id") if p else None


def current_user_email(
    authorization: str | None = Header(default=None),
) -> str | None:
    """The authenticated user's email, when the JWT carried one (or
    `API_KEY_FALLBACK_EMAIL` for the legacy path). None otherwise.
    """
    p = current_principal(authorization=authorization)
    return p.get("email") if p else None


def current_user_role(
    authorization: str | None = Header(default=None),
) -> str | None:
    """The authenticated principal's role, sourced from the JWT
    (`app_metadata.role` preferred, then the top-level `role`
    claim) or 'admin' for the legacy API_KEY path during cutover.

    Phase 5 sits on top of this dependency: admin endpoints use
    `require_admin` which ALSO checks the Supabase
    `public.user_roles.role` table (cached 5 min in-process) so
    an operator can grant admin via the dashboard without minting
    a new JWT with custom `app_metadata`.
    """
    p = current_principal(authorization=authorization)
    return p.get("role") if p else None


def require_admin(
    principal: dict | None = Depends(current_principal),
) -> dict:
    """Phase 5: admin-only gate.

    Resolution order:
      1. Legacy API_KEY auth path -> principal['role'] == 'admin'
         (set by `_api_key_fallback_principal`). Passes during the
         cutover window so admin endpoints work BEFORE Supabase
         user_roles is populated. Operator removes the fallback
         once real admin users are minted.
      2. JWT path -> consult Supabase `public.user_roles.role` via
         `core.supabase_admin.is_admin`. The lookup is cached in-
         process for 5 minutes per user_id.
      3. Anything else -> 403.

    Returns the validated principal dict so admin endpoints have
    `user_id` / `email` to log + display without re-resolving.
    """
    if principal is None:
        # Nobody authed at all -> 401 takes precedence over 403.
        raise HTTPException(401, "missing bearer token")
    if principal.get("role") == "admin":
        return principal
    # JWT path: check Supabase user_roles. Cached.
    from core.supabase_admin import is_admin
    if is_admin(principal.get("user_id")):
        return principal
    raise HTTPException(
        403,
        "admin role required; ask an existing admin to grant access "
        "via public.user_roles in the Supabase dashboard",
    )


def _ws_path() -> str:
    """Return the workspace path for the active request.

    Per-user mode (`WORKSPACE_PER_USER=true` and the request has an
    authenticated principal): returns `${WORKSPACES_ROOT}/{user_id}/`,
    provisioned from the template on first call. This is the path
    every mutating CLI shell-out uses, so they all stay in the
    tenant's own workspace.

    Legacy single-tenant mode: returns `INVESTOR_WORKSPACE` env var.
    Refuses to start with 500 if unset.
    """
    user_id = _CURRENT_USER_ID_VAR.get()
    if user_id and _per_user_workspaces_enabled():
        ws_path = _user_workspace_path(user_id)
        _provision_user_workspace(ws_path)
        return str(ws_path)
    ws = os.environ.get("INVESTOR_WORKSPACE")
    if not ws:
        raise HTTPException(
            500,
            "server misconfigured: INVESTOR_WORKSPACE env var is not set",
        )
    return ws


# ---------- per-user workspace resolution (Phase 2a) ----------

# Default values match the Fly deploy shape: `/data/workspaces/{uuid}/`
# is the per-tenant tree; `clients/test_workspace` is the template
# we copytree from on first auth. Locally, tests + dev override both
# via env so nothing lands in /data/.
_WORKSPACES_ROOT_DEFAULT = "/data/workspaces"
_WORKSPACE_TEMPLATE_DEFAULT = "clients/test_workspace"

# Anchor user_id to a safe slug regex -- Supabase emits UUIDs which
# are alnum + dashes, but a malicious or buggy token could carry
# anything in `sub`. We refuse to use the raw value as a path
# component if it doesn't match this shape.
import re  # noqa: E402

_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _per_user_workspaces_enabled() -> bool:
    """Opt-in switch for the Phase 2a per-user routing.

    Default off so existing single-tenant deployments + the entire
    test suite (which pins `INVESTOR_WORKSPACE` to the fixture
    workspace) keep working unchanged. Operators flip this on after
    setting `WORKSPACES_ROOT` and `WORKSPACE_TEMPLATE` to real
    paths on the Fly volume.
    """
    raw = os.environ.get("WORKSPACE_PER_USER", "")
    return raw.lower() in {"1", "true", "yes", "on"}


def _workspaces_root() -> pathlib.Path:
    raw = os.environ.get("WORKSPACES_ROOT") or _WORKSPACES_ROOT_DEFAULT
    return pathlib.Path(raw)


def _workspace_template() -> pathlib.Path:
    """Source path that `_provision_user_workspace` copytrees from on
    first auth. The default ships every workspace with the same
    config skeleton + empty examples; the user's wizard run then
    overwrites the relevant fields via PUT /config/company etc.
    """
    raw = os.environ.get("WORKSPACE_TEMPLATE") or _WORKSPACE_TEMPLATE_DEFAULT
    p = pathlib.Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p


def _user_workspace_path(user_id: str) -> pathlib.Path:
    """Return the per-user workspace path for `user_id`. Does NOT
    create it -- pair with `_provision_user_workspace` when you need
    a guaranteed-existing tree."""
    if not _USER_ID_RE.match(user_id or ""):
        raise HTTPException(
            400,
            f"user_id {user_id!r} is not a valid path slug "
            f"(expected alnum + dash/underscore, 1-64 chars)",
        )
    return _workspaces_root() / user_id


def _provision_user_workspace(dst: pathlib.Path) -> None:
    """Copytree the configured template into `dst` and wipe the
    pipeline.db so the new tenant starts with an empty database +
    a clean config skeleton. Idempotent on the target's existence:
    if `dst` already exists, this is a no-op (the engine open will
    handle schema migrations on an existing db).

    Provisioning failures bubble up as 500s -- the user sees the
    failure rather than silently getting a default-tenant fallback.
    """
    import shutil  # noqa: PLC0415

    if dst.exists():
        return
    template = _workspace_template()
    if not template.exists():
        raise HTTPException(
            500,
            f"workspace template {template} is missing on disk; "
            f"set WORKSPACE_TEMPLATE to a real path or deploy the "
            f"template tree alongside the API.",
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dst)
    # Don't carry the template's pipeline.db -- each user starts
    # with a fresh SQLite (tables get created on first engine open
    # via metadata.create_all).
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()


def _engine_and_ws():
    """Load workspace + engine.

    Phase 2a: when the active request authenticates via a Supabase
    JWT (or the legacy API_KEY with `API_KEY_FALLBACK_USER_ID`
    set), the workspace path is `${WORKSPACES_ROOT}/${user_id}/` --
    auto-provisioned from `${WORKSPACE_TEMPLATE}` on first auth.

    When no principal is available (legacy single-tenant mode,
    tests pinned via `INVESTOR_WORKSPACE`), falls back to the
    pinned env var. This preserves the existing test fixture story
    AND every pre-Phase-2 deployment that hasn't wired Supabase
    yet -- nothing breaks the day this code lands.

    Routing is via the contextvar populated by `current_principal`
    inside FastAPI's dependency-injection machinery. Endpoints that
    declare `Depends(current_principal)` (or any dependency that
    transitively calls it) get the per-user routing automatically;
    endpoints that don't take an authenticated dependency continue
    to use the pinned path.

    Not cached -- engine creation is cheap; caching across requests
    risks stale config when files on disk change out-of-band.
    """
    user_id = _CURRENT_USER_ID_VAR.get()
    if user_id and _per_user_workspaces_enabled():
        ws_path = _user_workspace_path(user_id)
        _provision_user_workspace(ws_path)
        ws = load_workspace(str(ws_path))
        return get_engine(ws.db_url), ws
    # No principal in context -> pinned-path legacy mode. Also
    # the path when per-user routing is opt-out (default) -- tests
    # and pre-Phase-2a deployments share one workspace.
    ws = load_workspace(_ws_path())
    return get_engine(ws.db_url), ws


# Contextvar populated by `current_principal` when it resolves a
# user_id (JWT path or env-bound API_KEY fallback). Read by
# `_engine_and_ws()` to scope the workspace per request without
# changing every endpoint signature.
#
# FastAPI runs each request in its own thread (or async task), so
# contextvars give us per-request isolation automatically. The
# previous all-singleton pattern (one INVESTOR_WORKSPACE for the
# process) was wrong the moment we added a second tenant.
import contextvars  # noqa: E402

_CURRENT_USER_ID_VAR: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("_current_user_id", default=None)
)


def _actor() -> str:
    return os.environ.get("API_OPERATOR", "api-client")


def _allow_example_domains_args() -> list[str]:
    """Expose fixture-domain bypass only when the API operator opts in.

    The CLI flag is useful for local/fixture demos, but the hosted
    API should not silently weaken production guards for browser
    clients.
    """
    raw = os.environ.get("API_ALLOW_EXAMPLE_DOMAINS", "")
    if raw.lower() in {"1", "true", "yes", "on"}:
        return ["--allow-example-domains"]
    return []


def _run_cli(
    *args: str, timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Shell out to scripts/<name>. The CLI scripts use the same
    workspace lock + audit log the operator path uses; the API just
    invokes them and surfaces the output.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / args[0]), *args[1:]]
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "USER": _actor()},
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )
