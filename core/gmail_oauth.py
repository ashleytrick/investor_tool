"""Web-friendly Gmail OAuth helpers used by the FastAPI backend.

The CLI script `scripts/connect_gmail.py` runs OAuth via
`InstalledAppFlow.run_local_server`, which spins up its own ephemeral
listener on a random localhost port. That works for a desktop terminal
but cannot drive a browser flow that originates in a remote dashboard.

This module factors the same OAuth into start/complete halves that
work with a remote redirect URI. The dashboard flow looks like:

  1. POST /gmail/connect
       -> start_flow(ws, redirect_uri) returns (auth_url, state)
       -> operator opens auth_url in their browser
  2. Operator consents on Google, browser redirects to:
       {redirect_uri}?code=...&state=...
  3. GET /oauth/gmail/callback?code=...&state=...
       -> complete_flow(state, code, ws) exchanges, writes the token
          to clients/<ws>/.gmail_token.json, returns the profile dict

The OAuth `state` parameter is minted inside an authenticated request
in step 1 and round-trips unmodified to step 3, so it works as a
one-time bearer for the callback (browsers cannot attach the API's
Bearer header across a Google redirect).

Token storage and the `gmail.compose` scope match `connect_gmail.py`
byte-for-byte, so a token written by either surface works with
`core.gmail_client.GmailClient` without further setup.

Note: this requires "Web application" OAuth client credentials in
GCP (with the API's `/oauth/gmail/callback` URL registered as an
authorized redirect URI). The Desktop-app credentials used by the
CLI flow will be rejected by Google for non-localhost redirects.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from core.gmail_client import SCOPES


@dataclass
class _PendingFlow:
    flow: object
    workspace_path: str  # absolute path; sanity-checked on callback


# Process-local store. Single-worker uvicorn (the API's deploy shape)
# means we don't need a shared backend; if we ever multi-worker, this
# moves to Redis / DB. Each entry is short-lived (a few minutes of
# operator browser time).
_lock = threading.Lock()
_pending: dict[str, _PendingFlow] = {}


def is_configured(ws) -> bool:
    """True if the workspace has OAuth client credentials on disk.

    Without these (downloaded from GCP), the OAuth flow can't even
    start. Surfaced separately from is_connected() so the wizard can
    distinguish "operator hasn't done the GCP setup" from "operator
    started OAuth but hasn't finished consent yet".
    """
    return (ws.path / ".gmail_credentials.json").exists()


_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _load_creds(ws):
    """Return a usable Credentials object, refreshing if stale, or
    None on any structural / refresh failure. Centralized so
    is_connected, drive_connected, and any future per-scope check
    share the same loading discipline."""
    token_path = ws.path / ".gmail_token.json"
    if not token_path.exists():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None
    try:
        creds = Credentials.from_authorized_user_file(
            str(token_path), SCOPES,
        )
    except Exception:  # noqa: BLE001 - bad token file -> treat as disconnected
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001 - refresh failure -> disconnected
            return None
        try:
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception:  # noqa: BLE001 - disk failure shouldn't poison the read
            pass
        return creds
    return None


def is_connected(ws) -> bool:
    """Truthy when a saved token can produce usable Credentials with
    the Gmail scope granted."""
    creds = _load_creds(ws)
    if creds is None:
        return False
    granted = set(getattr(creds, "scopes", []) or [])
    return _GMAIL_SCOPE in granted


def drive_connected(ws) -> bool:
    """Truthy when the same OAuth token also covers the Drive scope.
    Separate from is_connected so the wizard can distinguish 'Gmail
    works but Drive isn't consented yet' -- typically when an
    operator linked Gmail BEFORE the Drive scope was added to SCOPES,
    and needs to re-run /gmail/connect to extend consent."""
    creds = _load_creds(ws)
    if creds is None:
        return False
    granted = set(getattr(creds, "scopes", []) or [])
    return _DRIVE_SCOPE in granted


def start_flow(ws, redirect_uri: str) -> tuple[str, str]:
    """Build a Google OAuth `Flow`, store it under its `state` token,
    and return (auth_url, state). The caller (the /gmail/connect
    endpoint) returns auth_url to the frontend; state stays server-side.
    """
    creds_path = ws.path / ".gmail_credentials.json"
    if not creds_path.exists():
        raise FileNotFoundError(
            f"missing {creds_path}; download the OAuth client JSON "
            f"from GCP console (Web application type, with "
            f"{redirect_uri} registered as an authorized redirect URI) "
            f"and save it there before connecting"
        )
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:  # pragma: no cover - dep is in pyproject
        raise RuntimeError(
            "google-auth-oauthlib not installed; run `uv sync`"
        ) from exc
    flow = Flow.from_client_secrets_file(
        str(creds_path), scopes=SCOPES, redirect_uri=redirect_uri,
    )
    # access_type=offline + prompt=consent forces Google to return a
    # refresh_token even on re-auth; without it, repeat connects only
    # return an access token and the next refresh fails silently.
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    with _lock:
        _pending[state] = _PendingFlow(
            flow=flow, workspace_path=str(ws.path),
        )
    return auth_url, state


def pending_workspace_path(state: str) -> str | None:
    """Look up the workspace_path stamped on a pending OAuth state
    WITHOUT consuming the pending entry.

    Used by the callback handler (post-#3-review) to load the right
    workspace BEFORE calling complete_flow -- the callback runs
    without a Bearer header (Google redirects the browser to it
    cross-origin), so we can't resolve the tenant via the JWT path.
    The state token itself is the unforgeable handle.
    """
    with _lock:
        entry = _pending.get(state)
        if entry is None:
            return None
        return entry.workspace_path


def complete_flow(state: str, code: str, ws) -> dict:
    """Exchange the OAuth `code` for tokens, persist them, and return
    the connected Gmail profile.

    Refuses if `state` is unknown (expired / forged) or if the
    workspace the callback resolved to doesn't match the one that
    started the flow -- that mismatch means the server is configured
    differently than when the flow started, and we'd otherwise write
    a token into the wrong workspace.
    """
    with _lock:
        pending = _pending.pop(state, None)
    if pending is None:
        raise ValueError("unknown or expired OAuth state")
    if str(ws.path) != pending.workspace_path:
        raise ValueError(
            "oauth state belongs to a different workspace than the "
            "one the API is currently pinned to"
        )
    flow = pending.flow
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path = ws.path / ".gmail_token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "google-api-python-client not installed; run `uv sync`"
        ) from exc
    service = build("gmail", "v1", credentials=creds)
    return service.users().getProfile(userId="me").execute()
