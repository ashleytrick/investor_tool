"""Supabase admin REST helpers (Phase 5).

Looks up `public.user_roles.role` via the Supabase REST API so the
backend can enforce admin / moderator / user gating on routes the
JWT itself can't authoritatively answer (Supabase's JWT only
carries the broad `authenticated` / `service_role` audience tag;
the app-level role lives in a Postgres table the operator manages
through the Supabase dashboard).

Env contract:
  - SUPABASE_URL     -- e.g. https://abc123.supabase.co
  - SUPABASE_SERVICE_ROLE_KEY -- the service-role JWT (NOT the anon
    key). Required to read user_roles past Supabase's row-level
    security. Never sent to the browser; lives on Fly secrets only.

Cache: in-process dict keyed by user_id, 5-minute TTL. Cleared via
`invalidate_role(user_id)` on a 401 the caller observes (the
process-local cache is the right granularity at this scale -- a
Redis layer would be over-engineered for a single operator API).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional


# Default TTL matches the spec ("cache for 5 min in-process keyed
# by user_id"). Overridable via env for tests that want short TTLs.
_DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class _CachedRole:
    """Cache entry. role=None means 'we already asked Supabase and
    the user has no row in user_roles' -- distinct from 'not yet
    asked', which is represented by the entry not being in the dict
    at all. Caching the None lets the second 403 return fast."""
    role: Optional[str]
    expires_at: float


_lock = threading.Lock()
_cache: dict[str, _CachedRole] = {}


def _ttl_seconds() -> float:
    raw = os.environ.get("SUPABASE_ROLE_CACHE_TTL")
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return float(_DEFAULT_TTL_SECONDS)


def _now() -> float:
    return time.monotonic()


def _from_cache(user_id: str) -> Optional[_CachedRole]:
    with _lock:
        entry = _cache.get(user_id)
        if entry is None:
            return None
        if entry.expires_at <= _now():
            # Expired -> evict + miss. Forces a fresh lookup.
            _cache.pop(user_id, None)
            return None
        return entry


def _put_cache(user_id: str, role: Optional[str]) -> None:
    with _lock:
        _cache[user_id] = _CachedRole(
            role=role,
            expires_at=_now() + _ttl_seconds(),
        )


def invalidate_role(user_id: str) -> None:
    """Drop the cached role for `user_id`.

    Called by the auth layer on 401 (per the spec: 'Invalidate on
    401') so a JWT rotation that flips a user's role takes effect
    on the next request rather than waiting up to 5 minutes.
    """
    with _lock:
        _cache.pop(user_id, None)


def clear_cache() -> None:
    """Test helper -- drops everything. Production code doesn't
    need this; the per-user invalidate is enough."""
    with _lock:
        _cache.clear()


def _supabase_env() -> tuple[Optional[str], Optional[str]]:
    return (
        os.environ.get("SUPABASE_URL") or None,
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None,
    )


def _fetch_role_from_supabase(user_id: str) -> Optional[str]:
    """Single REST call to Supabase. Returns the role string when
    a row exists for the user_id, None otherwise (or on any
    failure -- we never raise; the caller treats None as 'not
    admin').

    Endpoint shape:
      GET {SUPABASE_URL}/rest/v1/user_roles?user_id=eq.{uid}&select=role&limit=1
      Authorization: Bearer {SUPABASE_SERVICE_ROLE_KEY}
      apikey: {SUPABASE_SERVICE_ROLE_KEY}
    """
    url, key = _supabase_env()
    if not url or not key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    endpoint = (
        f"{url.rstrip('/')}/rest/v1/user_roles"
        f"?user_id=eq.{user_id}&select=role&limit=1"
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(endpoint, headers=headers)
    except Exception:  # noqa: BLE001 - network failure -> treat as no role
        return None
    if resp.status_code >= 400:
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - bad payload -> treat as no role
        return None
    if not isinstance(body, list) or not body:
        return None
    row = body[0]
    if not isinstance(row, dict):
        return None
    role = row.get("role")
    if not isinstance(role, str) or not role:
        return None
    return role


def get_user_role(user_id: str) -> Optional[str]:
    """Cached read of the user's role.

    Public API for the rest of the backend. Returns:
      - the role string ('admin', 'moderator', 'user', ...) when
        Supabase has a row,
      - None when the user has no user_roles row, OR when Supabase
        is unreachable / unconfigured.

    Callers MUST NOT treat None as a security signal -- it's a
    cached-miss / no-config marker, not a deny. The admin gate
    checks `role == 'admin'` explicitly so None defaults to non-
    admin without overloading the meaning.
    """
    if not user_id:
        return None
    cached = _from_cache(user_id)
    if cached is not None:
        return cached.role
    role = _fetch_role_from_supabase(user_id)
    _put_cache(user_id, role)
    return role


def is_admin(user_id: Optional[str]) -> bool:
    """Convenience: True iff the user has the admin role in
    Supabase. None / no role / cache miss without Supabase = False.
    """
    if not user_id:
        return False
    return get_user_role(user_id) == "admin"


# ---------- Phase 6: Google identity fetch ----------

@dataclass(frozen=True)
class GoogleIdentity:
    """The bits of a user's Google identity the Gmail bootstrap
    needs: the provider tokens + the granted scope set + the
    user's Google email. All optional because Supabase may have
    a row for the user but not the Google identity (e.g. they
    signed up with email/password)."""
    email: Optional[str]
    provider_access_token: Optional[str]
    provider_refresh_token: Optional[str]
    scopes: list[str]


def _parse_scopes(scope_value: object) -> list[str]:
    """Supabase's identity_data.provider_token surface stores
    granted scopes as a single space-delimited string; some SDKs
    return a list. Normalize to a list."""
    if isinstance(scope_value, list):
        return [str(s) for s in scope_value if isinstance(s, str)]
    if isinstance(scope_value, str):
        return [s for s in scope_value.split() if s]
    return []


def fetch_google_identity(user_id: str) -> Optional[GoogleIdentity]:
    """GET the Supabase user via the admin API, pluck out the
    Google identity row's provider tokens + scopes + email.

    Returns None when:
      - Supabase is unconfigured (no URL / no service-role key)
      - the user doesn't exist
      - the user has no Google identity

    Errors collapse to None on purpose -- the caller decides whether
    to 409 (no refresh token) vs 403 (insufficient scope) vs 5xx,
    and we don't want a transient network blip to look like a
    permanent state change.
    """
    if not user_id:
        return None
    url, key = _supabase_env()
    if not url or not key:
        return None
    try:
        import httpx
    except ImportError:
        return None
    endpoint = f"{url.rstrip('/')}/auth/v1/admin/users/{user_id}"
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(endpoint, headers=headers)
    except Exception:  # noqa: BLE001 - network failure -> None
        return None
    if resp.status_code >= 400:
        return None
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    identities = body.get("identities") or []
    if not isinstance(identities, list):
        return None
    for identity in identities:
        if not isinstance(identity, dict):
            continue
        if identity.get("provider") != "google":
            continue
        idata = identity.get("identity_data") or {}
        if not isinstance(idata, dict):
            idata = {}
        return GoogleIdentity(
            email=(
                (idata.get("email") if isinstance(idata.get("email"), str) else None)
                or (body.get("email") if isinstance(body.get("email"), str) else None)
            ),
            provider_access_token=(
                idata.get("provider_token")
                if isinstance(idata.get("provider_token"), str) else None
            ),
            provider_refresh_token=(
                idata.get("provider_refresh_token")
                if isinstance(idata.get("provider_refresh_token"), str) else None
            ),
            scopes=_parse_scopes(idata.get("scopes") or idata.get("scope")),
        )
    return None
