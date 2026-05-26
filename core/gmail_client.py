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

SCOPES = [
    # Read / create / update / delete the operator's own drafts. Cannot
    # send mail -- the brief is explicit that the human stays in the
    # send loop.
    "https://www.googleapis.com/auth/gmail.compose",
    # Build Session 13: write meeting-prep briefs into the operator's
    # Drive. drive.file (not the broader drive scope) only grants
    # access to files this app creates or opens; we cannot read or
    # touch anything else in the operator's Drive.
    "https://www.googleapis.com/auth/drive.file",
]


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

    @classmethod
    def from_workspace_polling(cls, ws) -> "GmailClient":
        """Read-only constructor for poll endpoints (B2 onwards).

        Unlike `from_workspace`, this does NOT require
        `.gmail_credentials.json` -- tokens minted by Phase 6's
        `/gmail/bootstrap` carry the client_id + client_secret
        inline (harvested from Supabase's Google OAuth client),
        so refresh works without a separate credentials file.

        Raises `FileNotFoundError` when the token itself is missing
        -- pollers catch this and treat it as "tenant hasn't
        connected Gmail yet, skip silently."
        """
        token_path = ws.path / ".gmail_token.json"
        if not token_path.exists():
            raise FileNotFoundError(str(token_path))
        return cls(
            credentials_path=ws.path / ".gmail_credentials.json",
            token_path=token_path,
        )

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

    def list_sent_since(self, after: "object") -> list[dict]:
        """List Gmail Sent messages with internalDate >= `after`.

        Returns a list of dicts the polling layer consumes (NOT
        Gmail SDK types -- keeps tests fixturable without the
        google libs installed):

            {
              "external_id": str,    # RFC 822 Message-ID header
              "thread_id":   str,
              "occurred_at": datetime (UTC),
              "recipient_email": str | None,
              "subject":     str | None,
              "body_snippet": str | None,
            }

        Uses Gmail's `q=in:sent after:<unix_ts>` shape so we don't
        page through unbounded history on a fresh workspace.
        """
        import datetime as _dt
        if isinstance(after, _dt.datetime):
            unix_ts = int(after.timestamp())
        else:
            unix_ts = int(after)
        try:
            list_resp = self.service.users().messages().list(
                userId="me",
                q=f"in:sent after:{unix_ts}",
                maxResults=200,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise GmailError(f"messages.list failed: {exc}") from exc
        msg_refs = list_resp.get("messages") or []
        out: list[dict] = []
        for ref in msg_refs:
            msg_id = ref.get("id")
            if not msg_id:
                continue
            try:
                full = self.service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=[
                        "Message-ID", "From", "To", "Cc",
                        "Bcc", "Subject", "Date",
                    ],
                ).execute()
            except Exception:  # noqa: BLE001
                # Skip the one message; don't tank the whole pass.
                continue
            payload = full.get("payload") or {}
            headers = {
                (h.get("name") or "").lower(): (h.get("value") or "")
                for h in (payload.get("headers") or [])
            }
            internal_ms = full.get("internalDate")
            try:
                occurred_at = _dt.datetime.fromtimestamp(
                    int(internal_ms) / 1000, tz=_dt.timezone.utc,
                )
            except (TypeError, ValueError):
                continue
            external_id = headers.get("message-id") or msg_id
            recipient_email = _extract_first_email(
                headers.get("to") or ""
            ) or _extract_first_email(headers.get("cc") or "")
            out.append({
                "external_id": external_id,
                "thread_id": full.get("threadId"),
                "occurred_at": occurred_at,
                "recipient_email": recipient_email,
                "subject": headers.get("subject"),
                "body_snippet": full.get("snippet") or None,
            })
        return out

    def list_replies_since(
        self, after: "object", *, thread_ids: list[str] | None = None,
    ) -> list[dict]:
        """List Gmail INBOX messages with internalDate >= `after`,
        optionally restricted to a known set of thread IDs (i.e.
        only fetch replies in conversations we've sent into).

        Returns the same dict shape as `list_sent_since` plus an
        `is_reply` flag (always True here; preserved for symmetry)
        and `unread` (Gmail's UNREAD label).
        """
        import datetime as _dt
        if isinstance(after, _dt.datetime):
            unix_ts = int(after.timestamp())
        else:
            unix_ts = int(after)
        q = f"in:inbox after:{unix_ts}"
        try:
            list_resp = self.service.users().messages().list(
                userId="me", q=q, maxResults=200,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise GmailError(f"messages.list failed: {exc}") from exc
        msg_refs = list_resp.get("messages") or []
        tracked: set[str] | None = (
            set(thread_ids) if thread_ids else None
        )
        out: list[dict] = []
        for ref in msg_refs:
            msg_id = ref.get("id")
            if not msg_id:
                continue
            try:
                full = self.service.users().messages().get(
                    userId="me", id=msg_id, format="metadata",
                    metadataHeaders=[
                        "Message-ID", "From", "To", "Subject",
                        "Date", "In-Reply-To", "References",
                    ],
                ).execute()
            except Exception:  # noqa: BLE001
                continue
            thread_id = full.get("threadId")
            if tracked is not None and thread_id not in tracked:
                continue
            payload = full.get("payload") or {}
            headers = {
                (h.get("name") or "").lower(): (h.get("value") or "")
                for h in (payload.get("headers") or [])
            }
            internal_ms = full.get("internalDate")
            try:
                occurred_at = _dt.datetime.fromtimestamp(
                    int(internal_ms) / 1000, tz=_dt.timezone.utc,
                )
            except (TypeError, ValueError):
                continue
            external_id = headers.get("message-id") or msg_id
            sender = _extract_first_email(headers.get("from") or "")
            label_ids = set(full.get("labelIds") or [])
            out.append({
                "external_id": external_id,
                "thread_id": thread_id,
                "occurred_at": occurred_at,
                # For replies, the partner is the *sender*. Store
                # it in the same recipient_email field so callers
                # don't have to branch on event_type.
                "recipient_email": sender,
                "subject": headers.get("subject"),
                "body_snippet": full.get("snippet") or None,
                "unread": "UNREAD" in label_ids,
                "is_reply": True,
            })
        return out

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


def _extract_first_email(raw_header: str) -> Optional[str]:
    """Pull the first `foo@bar.tld` substring out of a To/Cc header
    value. Gmail returns these as `Display Name <foo@bar.tld>` or
    comma-separated lists; we just want the address."""
    import re
    if not raw_header:
        return None
    m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw_header)
    return m.group(0).lower() if m else None
