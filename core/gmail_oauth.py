"""Web-flow Gmail OAuth helpers.

`scripts/connect_gmail.py` uses `InstalledAppFlow.run_local_server`
which spawns a local HTTP listener and pops a browser -- great for
a CLI on the operator's laptop, doesn't translate to a deployed
FastAPI server. This module is the web-flow counterpart used by
`web/api.py`:

    start_flow(...) -> (auth_url, state) opaque token
                       redirects the user to Google's consent screen
    complete_flow(...) -> writes the token file the existing CLI
                          scripts already expect at
                          `<workspace>/.gmail_token.json`

State is held server-side (single-process in-memory dict with a
short TTL) so the callback can re-create the Flow with the same
client_secrets + redirect_uri the original request used. If the
machine restarts mid-OAuth, the state is gone -- user re-clicks
"Connect Gmail" and we start over. Acceptable for a single-
operator app; revisit if we ever serve multiple operators.

The OAuth client in Google Cloud Console must be of type
"Web application" (NOT Desktop) and must include the deployed
callback URL in its Authorized redirect URIs list. See
`docs/GMAIL_OAUTH.md` for the one-time GCP setup.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core.gmail_client import SCOPES


@dataclass(frozen=True)
class PendingFlow:
    credentials_path: str
    token_path: str
    redirect_uri: str
    created_at: float


# Server-side state. Keyed by the opaque CSRF token returned to the
# browser; the browser sends it back on the callback. TTL'd so a
# never-completed flow doesn't leak forever.
_STATE: dict[str, PendingFlow] = {}
_STATE_LOCK = threading.Lock()
_TTL_SECONDS = 600  # 10 min -- generous; users get distracted.


def _gc() -> None:
    """Drop expired entries. Called on every read so we don't grow
    unbounded under a never-completing-flow attack."""
    now = time.time()
    with _STATE_LOCK:
        stale = [
            k for k, v in _STATE.items()
            if now - v.created_at > _TTL_SECONDS
        ]
        for k in stale:
            del _STATE[k]


def start_flow(
    *,
    credentials_path: Path,
    token_path: Path,
    redirect_uri: str,
) -> tuple[str, str]:
    """Begin the web OAuth flow.

    Returns (auth_url, state). Caller (the API) returns auth_url
    to the browser as a redirect target. When Google calls back to
    `redirect_uri?code=...&state=...`, the same `state` is passed
    back; `complete_flow` looks it up and finishes the exchange.

    Raises FileNotFoundError if the client_secrets JSON isn't on
    disk -- the operator hasn't uploaded their GCP OAuth client
    credentials yet.
    """
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"OAuth client credentials not found at {credentials_path}. "
            f"Upload your Google Cloud Console OAuth client JSON to that "
            f"path first."
        )
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(credentials_path),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    state = secrets.token_urlsafe(32)
    _gc()
    with _STATE_LOCK:
        _STATE[state] = PendingFlow(
            credentials_path=str(credentials_path),
            token_path=str(token_path),
            redirect_uri=redirect_uri,
            created_at=time.time(),
        )
    return auth_url, state


def complete_flow(*, code: str, state: str) -> dict:
    """Finish the web OAuth flow on callback.

    Exchanges `code` for tokens using the same client_secrets +
    redirect_uri the original `start_flow` used. Writes the token
    JSON to the workspace's `.gmail_token.json` (where the existing
    `GmailClient` reads it). Returns the connected Gmail profile
    dict for the caller to confirm to the user.

    Raises:
      KeyError    -- `state` not found (expired or never started).
      ValueError  -- token exchange failed (bad code, redirect_uri
                     mismatch, scope mismatch, etc.).
    """
    _gc()
    with _STATE_LOCK:
        pending = _STATE.pop(state, None)
    if pending is None:
        raise KeyError(
            "state token not found or expired -- restart the "
            "connect-gmail flow"
        )
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        pending.credentials_path,
        scopes=SCOPES,
        redirect_uri=pending.redirect_uri,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - diverse google errors
        raise ValueError(f"token exchange failed: {exc}") from exc
    creds = flow.credentials
    token_path = Path(pending.token_path)
    token_path.write_text(creds.to_json(), encoding="utf-8")

    # Confirm by hitting users.getProfile (cheapest read in the
    # gmail.compose scope) so we know the token actually works.
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)
    return service.users().getProfile(userId="me").execute()


def token_valid(token_path: Path) -> bool:
    """True iff a token file exists at `token_path` and can be
    loaded as Google credentials. Doesn't hit the network -- a
    stale token returns True here but will refresh-or-fail on the
    next live API call. That's fine for a connectivity badge in
    the UI."""
    if not token_path.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials
        Credentials.from_authorized_user_file(str(token_path), SCOPES)
        return True
    except Exception:  # noqa: BLE001
        return False
