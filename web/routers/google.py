"""Google OAuth endpoints (Gmail + Drive).

Four routes extracted from `web/api.py`:

  GET  /gmail/status            -- legacy single-scope status
  GET  /google/status           -- per-scope (gmail + drive) status
  POST /gmail/connect           -- start the browser OAuth flow
  GET  /oauth/gmail/callback    -- Google redirects here after consent

Paths and behavior are byte-identical to the pre-extraction
versions; the only difference is that the routes live on an
APIRouter that `web/api.py` includes at import time.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core import gmail_oauth
from web.deps import _engine_and_ws, require_auth


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
