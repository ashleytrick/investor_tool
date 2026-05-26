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

from fastapi import HTTPException, Header

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config_loader import load_workspace  # noqa: E402
from core.db import get_engine  # noqa: E402


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
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()

    jwt_mode = _jwt_secret() is not None

    # 1) Supabase JWT path (when configured).
    if jwt_mode and _verify_supabase_jwt(token) is not None:
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

    Pure read; does NOT raise. `require_auth` is the gate.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()

    if _jwt_secret() is not None:
        claims = _verify_supabase_jwt(token)
        if claims is not None:
            principal = _principal_from_claims(claims)
            return principal or None

    # API_KEY path -- only honored when the env var binds it to a
    # specific user_id.
    fallback_on = (
        _jwt_secret() is None or _is_api_key_fallback_enabled()
    )
    if fallback_on:
        expected = os.environ.get("API_KEY") or ""
        if expected and hmac.compare_digest(token, expected):
            return _api_key_fallback_principal()

    return None


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

    The per-tenant Supabase `public.user_roles.role` lookup that
    upgrades 'authenticated' -> 'admin' / 'moderator' is Phase 5
    work and goes on top of this dependency.
    """
    p = current_principal(authorization=authorization)
    return p.get("role") if p else None


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
