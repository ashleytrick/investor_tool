"""Thin Gmail wrapper for creating DRAFTS (not sends).

OAuth setup (one-time, per operator):
  1. Create a GCP project, enable the Gmail API.
  2. Create OAuth client credentials (Desktop app type).
  3. Download as JSON, save to clients/{workspace}/.gmail_credentials.json.
  4. First run of create_gmail_drafts.py opens a browser for consent and
     writes the refresh token to clients/{workspace}/.gmail_token.json.

Scope: gmail.compose (create / read / update / delete OWN drafts; cannot send).
"""
from __future__ import annotations

import base64
import pathlib
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Optional

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


class GmailError(RuntimeError):
    """Raised on any Gmail API error."""


class GmailNotConfigured(RuntimeError):
    """Raised when the workspace has no .gmail_credentials.json on disk."""


@dataclass
class GmailClient:
    credentials_path: pathlib.Path
    token_path: pathlib.Path
    _service: Optional[object] = None

    @classmethod
    def from_workspace(cls, ws) -> "GmailClient":
        creds_path = ws.path / ".gmail_credentials.json"
        token_path = ws.path / ".gmail_token.json"
        if not creds_path.exists():
            raise GmailNotConfigured(
                f"missing {creds_path}; download OAuth client JSON from GCP "
                f"console and save it there"
            )
        return cls(credentials_path=creds_path, token_path=token_path)

    @property
    def service(self):
        if self._service is not None:
            return self._service
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailError(
                "google API client libraries not installed; run `uv sync`"
            ) from exc

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), SCOPES,
            )
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES,
                )
                creds = flow.run_local_server(port=0)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def get_profile(self) -> dict:
        """Slice 15 Gmail discoverability check.

        Calls `users.getProfile` -- the cheapest read-only Gmail API
        call available with the `gmail.compose` scope. Used by
        `scripts/check_ready.py` to confirm the OAuth token is still
        valid + the account is reachable BEFORE the operator gets
        all the way to draft-push time. Returns the raw dict
        (`emailAddress`, `messagesTotal`, `threadsTotal`,
        `historyId`); raises GmailError on any API failure.
        """
        try:
            return self.service.users().getProfile(userId="me").execute()
        except Exception as exc:  # noqa: BLE001 - diverse google errors
            raise GmailError(f"get_profile failed: {exc}") from exc

    def create_draft(
        self, *, to_email: str, subject: str, body: str,
        from_email: Optional[str] = None,
    ) -> tuple[str, str]:
        """Returns (draft_id, draft_url) for the operator to open in Gmail."""
        msg = MIMEText(body)
        msg["to"] = to_email
        msg["subject"] = subject
        if from_email:
            msg["from"] = from_email
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        try:
            draft = self.service.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw}},
            ).execute()
        except Exception as exc:  # noqa: BLE001 - Google client raises diverse types
            raise GmailError(f"create_draft failed: {exc}") from exc
        draft_id = draft.get("id")
        url = f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}"
        return draft_id, url
