"""Thin Drive wrapper for pushing meeting-prep briefs to the
operator's Drive.

OAuth: shares credentials + token with `core/gmail_client.py`. The
combined OAuth flow (`core/gmail_oauth.py`) requests both
gmail.compose and drive.file in one consent step -- the operator
sees "Connect Google" in the wizard, not "Connect Gmail" + "Connect
Drive" separately.

Scope: `drive.file` only. That grants access ONLY to files this app
creates or explicitly opens via a Drive picker. We CANNOT read,
list, or modify anything else in the operator's Drive -- the
narrowest scope that lets us push a Google Doc out to their account.

Folder layout: every brief lands inside an `investor_outreach/briefs`
folder created in the operator's Drive root on first push. Subsequent
pushes reuse the same folder.
"""
from __future__ import annotations

import io
import pathlib
from dataclasses import dataclass, field
from typing import Optional

from core.gmail_client import SCOPES

# Drive query helpers used to find the folder by name.
_BRIEFS_FOLDER_NAME = "investor_outreach_briefs"
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


class DriveError(RuntimeError):
    """Any Drive API failure surfaces here."""


class DriveNotConfigured(RuntimeError):
    """Raised when the workspace has no `.gmail_credentials.json`."""


@dataclass
class DriveClient:
    credentials_path: pathlib.Path
    token_path: pathlib.Path
    _service: Optional[object] = None
    _folder_id: Optional[str] = field(default=None, init=False, repr=False)

    @classmethod
    def from_workspace(cls, ws) -> "DriveClient":
        creds_path = ws.path / ".gmail_credentials.json"
        token_path = ws.path / ".gmail_token.json"
        if not creds_path.exists():
            raise DriveNotConfigured(
                f"missing {creds_path}; download OAuth client JSON from "
                f"GCP and save it there"
            )
        return cls(credentials_path=creds_path, token_path=token_path)

    @property
    def service(self):
        """Lazily build the Drive service, refreshing the token if the
        saved one is expired. Mirrors core/gmail_client.GmailClient.
        """
        if self._service is not None:
            return self._service
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveError(
                "google API client libraries not installed; run `uv sync`"
            ) from exc

        if not self.token_path.exists():
            raise DriveError(
                f"no OAuth token at {self.token_path}; run "
                f"`scripts/connect_gmail.py` first (or hit "
                f"/gmail/connect from the wizard) -- the Drive scope "
                f"is bundled into the same OAuth flow"
            )
        creds = Credentials.from_authorized_user_file(
            str(self.token_path), SCOPES,
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise DriveError(
                    "OAuth token is invalid and not refreshable; "
                    "re-run /gmail/connect"
                )
        # If the operator originally consented to gmail.compose only
        # (before the Drive scope was added), the token won't have the
        # drive.file scope even though it's listed in SCOPES locally.
        # The Drive upload call would then 403; surface a clearer
        # error here.
        granted = set(getattr(creds, "scopes", []) or [])
        if "https://www.googleapis.com/auth/drive.file" not in granted:
            raise DriveError(
                "OAuth token does not include drive.file scope; "
                "re-run /gmail/connect to grant the Drive scope "
                "(the wizard's 'Connect Google' button)"
            )
        self._service = build("drive", "v3", credentials=creds)
        return self._service

    # --- folder management -------------------------------------------------

    def briefs_folder_id(self) -> str:
        """Return the Drive folder id for `investor_outreach_briefs`,
        creating it on first call. Cached on the instance so a single
        DriveClient pushing many briefs only does one round-trip."""
        if self._folder_id is not None:
            return self._folder_id
        try:
            results = self.service.files().list(
                q=(
                    f"mimeType = '{_DRIVE_FOLDER_MIME}' "
                    f"and name = '{_BRIEFS_FOLDER_NAME}' "
                    f"and trashed = false"
                ),
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
            ).execute()
        except Exception as exc:  # noqa: BLE001 - Google client raises diverse types
            raise DriveError(f"folder lookup failed: {exc}") from exc

        files = results.get("files", [])
        if files:
            self._folder_id = files[0]["id"]
            return self._folder_id
        try:
            folder = self.service.files().create(
                body={
                    "name": _BRIEFS_FOLDER_NAME,
                    "mimeType": _DRIVE_FOLDER_MIME,
                },
                fields="id",
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"folder create failed: {exc}") from exc
        self._folder_id = folder["id"]
        return self._folder_id

    # --- uploads -----------------------------------------------------------

    def upload_brief(
        self, *, filename: str, markdown_text: str,
    ) -> tuple[str, str]:
        """Upload `markdown_text` as a Google Doc named `filename` into
        the briefs folder. Returns (doc_id, web_view_url).

        Idempotency note: we always CREATE a new doc rather than
        updating an existing one with the same name. Drive allows
        multiple files with the same name in the same folder, so the
        caller controls dedup at the filename layer (the meeting_prep
        integration uses `{partner_id}_{signal_hash}` which is
        idempotent on its own -- one doc per (partner, signal set)).
        """
        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ImportError as exc:
            raise DriveError(
                "google API client libraries not installed; run `uv sync`"
            ) from exc

        folder_id = self.briefs_folder_id()
        media = MediaIoBaseUpload(
            io.BytesIO(markdown_text.encode("utf-8")),
            mimetype="text/markdown",
            resumable=False,
        )
        try:
            doc = self.service.files().create(
                # Tell Drive to convert the upload into a native
                # Google Doc. Without this, the file lands as a
                # raw .md attachment.
                body={
                    "name": filename,
                    "mimeType": _GOOGLE_DOC_MIME,
                    "parents": [folder_id],
                },
                media_body=media,
                fields="id, webViewLink",
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"upload failed: {exc}") from exc
        return doc["id"], doc.get("webViewLink", "")

    def get_about(self) -> dict:
        """Cheap connectivity check. `about.get(fields=user)` is the
        minimum-cost Drive call; used by /drive/status to confirm the
        token is still valid + the operator's email is reachable."""
        try:
            return self.service.about().get(fields="user").execute()
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"about.get failed: {exc}") from exc
