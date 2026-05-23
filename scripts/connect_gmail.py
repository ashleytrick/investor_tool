"""One-time-per-workspace flow to link your Gmail so create_gmail_drafts.py
can write drafts on your behalf.

Three states the script handles:

  1. NO credentials yet -> prints the GCP setup walkthrough and exits 2.
  2. Credentials present, no token (or expired/invalid token) -> opens
     browser, runs OAuth, writes token, confirms which Gmail account
     connected via gmail.users().getProfile.
  3. Credentials + valid token -> just reports connection status; --force
     re-runs the OAuth flow (useful if you switched accounts).

Disconnect: `scripts/connect_gmail.py --disconnect` removes the token (keeps
credentials so reconnect is fast).

Scope: gmail.compose only -- create / read / update / delete OWN drafts.
NEVER send. The brief is explicit that the human stays in the send loop.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine
from core.gmail_client import SCOPES
from core.runs import RunLogger

STAGE = "connect_gmail"

SETUP_INSTRUCTIONS = """\
Gmail isn't linked to this workspace yet. One-time GCP setup (~5 min):

  1. Go to https://console.cloud.google.com/ and create a project (or pick
     an existing one). Name it whatever you want; only you ever see it.

  2. Enable the Gmail API for that project:
     https://console.cloud.google.com/apis/library/gmail.googleapis.com
     -> click Enable.

  3. Configure the OAuth consent screen (one-time per project):
     https://console.cloud.google.com/apis/credentials/consent
     - User Type: External
     - App name: anything (e.g. "investor outreach drafts")
     - User support email + Developer email: your email
     - Scopes: you can leave default; we request gmail.compose at runtime
     - Test users: add YOUR Gmail address (otherwise OAuth refuses you)
     - Save. You don't need to "publish" the app; "Testing" mode is fine
       for personal use (up to 100 test users, tokens last 7 days but
       refresh transparently while the app is in Testing).

  4. Create an OAuth client ID:
     https://console.cloud.google.com/apis/credentials
     - Create credentials -> OAuth client ID
     - Application type: Desktop app
     - Name: anything
     - Download the JSON.

  5. Save the downloaded JSON to:
     {creds_path}

  Then re-run:
     uv run scripts/connect_gmail.py --workspace clients/{ws_name}

What this lets the tool do: create drafts in YOUR Gmail Drafts folder.
What this does NOT let the tool do: send email. The gmail.compose scope
literally has no send permission; you click send yourself.

If you'd rather skip Gmail entirely, the CSV path still works:
  uv run scripts/07_generate_emails.py
  -> clients/{ws_name}/exports/review_queue.csv
"""


def _do_oauth(creds_path: pathlib.Path, token_path: pathlib.Path) -> dict:
    """Run the OAuth flow + return the connected profile dict."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), SCOPES
            )
        except Exception:  # noqa: BLE001 - bad token file -> redo OAuth
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Link Gmail for create_gmail_drafts.py."
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--force", action="store_true",
        help="Re-run OAuth even if a valid token exists (useful when "
             "switching the connected account).",
    )
    parser.add_argument(
        "--disconnect", action="store_true",
        help="Remove the saved token (keeps credentials so reconnect is fast).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    creds_path = ws.path / ".gmail_credentials.json"
    token_path = ws.path / ".gmail_token.json"

    with RunLogger(engine, ws.name, STAGE) as run:
        if args.disconnect:
            if token_path.exists():
                token_path.unlink()
                print(f"[connect_gmail] removed {token_path.name}")
                run.note("disconnected")
                run.succeeded = 1
            else:
                print(f"[connect_gmail] no token to remove at {token_path}")
                run.skipped = 1
            return 0

        if not creds_path.exists():
            print(
                SETUP_INSTRUCTIONS.format(
                    creds_path=creds_path, ws_name=ws.name,
                )
            )
            run.skipped = 1
            run.note("credentials missing; showed setup instructions")
            return 2

        if token_path.exists() and not args.force:
            # Already linked? Confirm by hitting getProfile and showing the
            # connected email. If the token refreshes silently, great; if it
            # raises, fall through to a real OAuth.
            try:
                profile = _do_oauth(creds_path, token_path)
                print(
                    f"[connect_gmail] already linked: "
                    f"{profile.get('emailAddress')} "
                    f"({profile.get('messagesTotal', '?')} messages total)"
                )
                print(
                    "[connect_gmail] re-run with --force to switch accounts, "
                    "or --disconnect to unlink."
                )
                run.note(f"already linked: {profile.get('emailAddress')}")
                run.succeeded = 1
                return 0
            except Exception as exc:  # noqa: BLE001 - fall through to fresh OAuth
                print(
                    f"[connect_gmail] existing token invalid ({exc}); "
                    f"re-running OAuth..."
                )
                token_path.unlink(missing_ok=True)

        # Fresh OAuth (or --force).
        if args.force and token_path.exists():
            token_path.unlink()

        try:
            profile = _do_oauth(creds_path, token_path)
        except Exception as exc:  # noqa: BLE001 - report cleanly
            print(f"[connect_gmail] FAILED: {exc}")
            run.failed = 1
            run.log_error("__oauth__", type(exc).__name__, str(exc))
            return 2

        print(
            f"[connect_gmail] linked {profile.get('emailAddress')!r}. "
            f"Token saved to {token_path.name}."
        )
        print(
            "[connect_gmail] next: "
            "uv run scripts/create_gmail_drafts.py"
        )
        run.note(f"linked {profile.get('emailAddress')}")
        run.succeeded = 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
