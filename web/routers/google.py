"""Google OAuth endpoints (Gmail + Drive).

Five routes:

  GET  /gmail/status            -- legacy single-scope status
  GET  /google/status           -- per-scope (gmail + drive) status
  POST /gmail/connect           -- start the browser OAuth flow
  GET  /oauth/gmail/callback    -- Google redirects here after consent
  POST /gmail/bootstrap         -- Phase 6: harvest the Google
                                   refresh token Supabase already
                                   has (when the operator signed in
                                   via Supabase OAuth with the gmail
                                   scope) so the wizard's connect
                                   step is one click instead of two.

Phase 6 (`/gmail/bootstrap`) makes the standard wizard signup also
cover the Gmail connection in the same OAuth round-trip: the
frontend asks Supabase for Google OAuth with
`scopes='https://www.googleapis.com/auth/gmail.send' access_type=offline
prompt=consent`, and the backend reads the resulting
`provider_refresh_token` from Supabase's admin API + persists it
at the workspace's standard `.gmail_token.json` path so the rest
of the Gmail surface (status, draft creation) works without a
second consent.

Paths and behavior of the original four routes are byte-identical
to the pre-extraction versions; the only difference is that the
routes live on an APIRouter that `web/api.py` includes at import
time.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core import gmail_oauth
from core import supabase_admin
from web.deps import _engine_and_ws, current_principal, require_auth


# ---------- response models ----------

class GmailStatus(BaseModel):
    """Legacy single-scope status. Kept for callers that haven't yet
    migrated to /google/status's per-scope shape."""
    connected: bool


class GmailConnectResponse(BaseModel):
    auth_url: str


class GoogleStatus(BaseModel):
    """Full per-scope OAuth status. The wizard polls this after the
    operator finishes consent so it can tell 'Gmail and Drive both
    granted' from 'Gmail granted, Drive needs re-consent'."""
    gmail_connected: bool
    drive_connected: bool
    google_connected: bool


class GmailBootstrapResult(BaseModel):
    """Response from POST /gmail/bootstrap. The frontend renders
    `connected: true` + the discovered email as the wizard's
    confirmation step."""
    connected: bool
    email: Optional[str] = None


# Required Gmail scope for draft creation. Matches the scope the
# existing CLI flow asks for (gmail.compose). Phase 6 doesn't
# touch gmail_client.SCOPES, so a Supabase OAuth that granted
# gmail.send is accepted IFF gmail.send subsumes the compose
# permission Drafts API uses. The check below also accepts
# gmail.compose for ops who set up Supabase OAuth narrowly.
_BOOTSTRAP_ACCEPTABLE_GMAIL_SCOPES = {
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    # gmail.modify / mail.google.com are broader and ALSO subsume
    # compose -- operators who chose those scopes during signup
    # don't need to re-consent for the narrower one.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",
}


router = APIRouter(tags=["onboarding"])


@router.get(
    "/gmail/status",
    response_model=GmailStatus,
    summary="Is Gmail OAuth completed for the pinned workspace?",
)
def gmail_status(
    _auth: None = Depends(require_auth),
) -> GmailStatus:
    _, ws = _engine_and_ws()
    return GmailStatus(connected=gmail_oauth.is_connected(ws))


@router.get(
    "/google/status",
    response_model=GoogleStatus,
    summary="Per-scope OAuth status (Gmail + Drive)",
)
def google_status(
    _auth: None = Depends(require_auth),
) -> GoogleStatus:
    """Build Session 13: the wizard renames the connect button to
    'Connect Google' because the OAuth flow now grants both Gmail
    (gmail.compose) and Drive (drive.file) scopes. This endpoint
    surfaces per-scope state so the wizard can distinguish 'fully
    connected' from 'Gmail granted, Drive needs re-consent' (the
    case where an operator linked Gmail before drive.file was added
    to SCOPES)."""
    _, ws = _engine_and_ws()
    gmail_ok = gmail_oauth.is_connected(ws)
    drive_ok = gmail_oauth.drive_connected(ws)
    return GoogleStatus(
        gmail_connected=gmail_ok,
        drive_connected=drive_ok,
        google_connected=gmail_ok and drive_ok,
    )


@router.post(
    "/gmail/connect",
    response_model=GmailConnectResponse,
    summary="Start Gmail OAuth; returns Google's auth URL",
)
def gmail_connect(
    request: Request,
    _auth: None = Depends(require_auth),
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


@router.get(
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

    Auth model: NO Bearer header (browsers can't attach custom
    headers to a cross-origin redirect from accounts.google.com).
    The `state` parameter -- minted server-side inside an
    authenticated /gmail/connect call -- works as a single-use
    bearer because it's cryptographically random and we delete it
    on first use. This is the standard OAuth CSRF / auth pattern,
    not a missing auth check.
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
    except Exception as exc:  # noqa: BLE001 - diverse Google SDK errors
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


@router.post(
    "/gmail/bootstrap",
    response_model=GmailBootstrapResult,
    summary=(
        "Harvest the Google refresh token Supabase already has + "
        "persist it as the workspace's Gmail token (single-step "
        "signup)"
    ),
)
def gmail_bootstrap(
    principal: dict | None = Depends(current_principal),
    _auth: None = Depends(require_auth),
) -> GmailBootstrapResult:
    """When the frontend signs the user in via Supabase OAuth
    with the gmail.send scope + `access_type=offline` +
    `prompt=consent`, Supabase stores the provider refresh token
    on the user's Google identity row. This endpoint reads it via
    the Supabase admin API and persists it at the workspace's
    standard `.gmail_token.json` path so the rest of the Gmail
    surface works without a second consent.

    Error contract:
      - 401 if the request isn't authenticated.
      - 409 `missing_refresh_token` -- Supabase has the user but
        no Google identity OR the identity has no refresh_token.
        The frontend falls back to the explicit `/gmail/connect`
        flow.
      - 403 `insufficient_scope` -- the granted Google scopes don't
        cover Gmail draft creation. The frontend should re-request
        OAuth with the right scope.
      - 500 if Supabase / google client credentials are
        unconfigured -- the operator hasn't completed the deploy
        steps and the bootstrap can't work yet.
    """
    if principal is None:
        # require_auth would normally catch this, but keep the
        # belt-and-braces 401 so the path stays explicit.
        raise HTTPException(401, "missing bearer token")

    user_id = principal.get("user_id")
    if not user_id:
        raise HTTPException(
            500,
            "authenticated principal carries no user_id; refusing "
            "to bootstrap Gmail to an unknown tenant",
        )

    identity = supabase_admin.fetch_google_identity(user_id)
    if identity is None or not identity.provider_refresh_token:
        raise HTTPException(
            409,
            detail={
                "error": "missing_refresh_token",
                "message": (
                    "no Google refresh token on file for this user. "
                    "Re-sign-in via Supabase Google OAuth with "
                    "access_type=offline + prompt=consent, or fall "
                    "back to /gmail/connect for the browser flow."
                ),
            },
        )
    if not _has_acceptable_scope(identity.scopes):
        raise HTTPException(
            403,
            detail={
                "error": "insufficient_scope",
                "granted_scopes": identity.scopes,
                "required_one_of": sorted(
                    _BOOTSTRAP_ACCEPTABLE_GMAIL_SCOPES
                ),
                "message": (
                    "the granted Google scopes do not include a "
                    "scope that allows Gmail draft creation. "
                    "Re-request OAuth with at least gmail.compose "
                    "(or gmail.send, gmail.modify, or "
                    "mail.google.com)."
                ),
            },
        )

    # We need Supabase's OWN Google OAuth client_id + secret to
    # refresh the access token later. These live on the Supabase
    # dashboard (Auth -> Providers -> Google); the operator
    # duplicates them as Fly secrets so the backend can refresh.
    client_id = os.environ.get("SUPABASE_GOOGLE_CLIENT_ID") or ""
    client_secret = (
        os.environ.get("SUPABASE_GOOGLE_CLIENT_SECRET") or ""
    )
    if not client_id or not client_secret:
        raise HTTPException(
            500,
            detail={
                "error": "supabase_google_client_unconfigured",
                "message": (
                    "the backend needs SUPABASE_GOOGLE_CLIENT_ID + "
                    "SUPABASE_GOOGLE_CLIENT_SECRET to refresh the "
                    "harvested provider tokens. Set them to the "
                    "same Google OAuth client Supabase uses, then "
                    "retry."
                ),
            },
        )

    _, ws = _engine_and_ws()
    token_path = ws.path / ".gmail_token.json"
    # Build a creds JSON google.oauth2.credentials.Credentials can
    # load directly. The access token is short-lived; the refresh
    # token + client credentials carry the long-lived authority.
    expiry_iso: Optional[str] = None
    if identity.provider_access_token:
        # The Supabase admin API doesn't surface the original
        # exp; assume a conservative 50-minute lifetime so the
        # refresh fires before Google would 401.
        expiry_iso = (
            datetime.now(timezone.utc) + timedelta(minutes=50)
        ).isoformat()
    token_payload = {
        "token": identity.provider_access_token,
        "refresh_token": identity.provider_refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": identity.scopes,
    }
    if expiry_iso:
        token_payload["expiry"] = expiry_iso
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps(token_payload, indent=2),
        encoding="utf-8",
    )

    return GmailBootstrapResult(
        connected=True,
        email=identity.email,
    )


def _has_acceptable_scope(scopes: list[str]) -> bool:
    """True iff at least one of the granted scopes covers Gmail
    draft creation. Acceptable scopes are the precise compose
    permission OR any broader scope that subsumes it."""
    if not scopes:
        return False
    granted = set(scopes)
    return bool(granted & _BOOTSTRAP_ACCEPTABLE_GMAIL_SCOPES)
