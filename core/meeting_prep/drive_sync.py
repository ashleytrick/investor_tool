"""Drive auto-push for the meeting-prep flow.

When the operator has connected Google (via the wizard's "Connect
Google" button, which now bundles Gmail + Drive scopes), every
prep_brief.py run that produces a fresh artifact also pushes the
rendered markdown to the operator's Drive as a native Google Doc.

Filename convention -- unique per (partner, signal_set):

    {partner_id}_{YYYY-MM-DD}_{signal_hash_short}

The hash suffix means: re-running prep_brief against the SAME signal
set always lands the same filename -- no clutter. A new signal flips
the hash and produces a new doc, so each version is a separate file
the operator can compare.

Idempotency: we record the (doc_id, url, timestamp) on the
meeting_prep_artifacts row that produced the doc. Subsequent
prep_brief runs that hit the cache see drive_doc_id is already set
and skip the upload entirely. Only the FIRST resolve-from-cache-miss
of a given signal_set hash hits Drive.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc, select, update
from sqlalchemy.engine import Engine

from core.db import meeting_prep_artifacts
from core.drive_client import DriveClient, DriveError, DriveNotConfigured
from core.gmail_oauth import drive_connected


@dataclass
class DrivePushResult:
    pushed: bool             # True iff we actually uploaded this call
    doc_id: str | None       # the Drive doc id (new OR previously cached)
    doc_url: str | None      # webViewLink
    skipped_reason: str | None = None


def filename_for_artifact(
    *, partner_id: str, signal_set_hash: str, artifact_type: str,
    today: datetime | None = None,
) -> str:
    """Build the Drive filename for one (partner, signal-set, type).

    Stable across re-runs against the same signal set -- the hash
    suffix encodes the inputs, so Drive only sees a new file when
    the verified evidence actually changed. The date prefix makes
    the brief sortable in Drive's UI without forcing the operator
    to read the hash.
    """
    today = today or datetime.now(timezone.utc)
    short = signal_set_hash[:8]
    return (
        f"{partner_id}__{today.strftime('%Y-%m-%d')}__"
        f"{artifact_type}__{short}"
    )


def push_if_needed(
    engine: Engine, ws, *, partner_id: str, signal_set_hash: str,
    artifact_type: str, markdown_text: str,
    drive_client_factory=None,
) -> DrivePushResult:
    """Push a rendered brief to Drive when conditions are met.

    Skipped (no-op) when:
      - the operator hasn't connected Google
      - the Drive scope wasn't granted (e.g. legacy gmail-only token)
      - this exact (partner_id, signal_set_hash, artifact_type)
        already has a drive_doc_id on file (idempotent re-runs)

    `drive_client_factory` is a hook for tests: callers can pass a
    fake that returns a stub DriveClient. Production code leaves it
    None and the default factory builds the real client.
    """
    if not drive_connected(ws):
        return DrivePushResult(
            pushed=False, doc_id=None, doc_url=None,
            skipped_reason="drive scope not granted on the saved OAuth token",
        )

    # Has this exact artifact already been pushed? If yes, skip.
    cached = _existing_drive_doc(
        engine,
        partner_id=partner_id,
        artifact_type=artifact_type,
        signal_set_hash=signal_set_hash,
    )
    if cached is not None and cached.doc_id:
        return DrivePushResult(
            pushed=False,
            doc_id=cached.doc_id, doc_url=cached.doc_url,
            skipped_reason="already pushed for this signal_set_hash",
        )

    factory = drive_client_factory or (lambda: DriveClient.from_workspace(ws))
    try:
        client = factory()
    except DriveNotConfigured as exc:
        return DrivePushResult(
            pushed=False, doc_id=None, doc_url=None,
            skipped_reason=f"drive not configured: {exc}",
        )

    filename = filename_for_artifact(
        partner_id=partner_id,
        signal_set_hash=signal_set_hash,
        artifact_type=artifact_type,
    )
    try:
        doc_id, url = client.upload_brief(
            filename=filename, markdown_text=markdown_text,
        )
    except DriveError as exc:
        # Don't propagate -- the brief on disk is still useful. Log
        # via the skipped_reason channel; the operator sees it in
        # the markdown prep_brief.py prints.
        return DrivePushResult(
            pushed=False, doc_id=None, doc_url=None,
            skipped_reason=f"drive upload failed: {exc}",
        )

    _stamp_drive_columns(
        engine,
        partner_id=partner_id,
        artifact_type=artifact_type,
        signal_set_hash=signal_set_hash,
        doc_id=doc_id, doc_url=url,
    )
    return DrivePushResult(pushed=True, doc_id=doc_id, doc_url=url)


@dataclass
class _ExistingDoc:
    doc_id: str
    doc_url: str | None


def _existing_drive_doc(
    engine: Engine, *, partner_id: str, artifact_type: str,
    signal_set_hash: str,
) -> _ExistingDoc | None:
    """Check whether the latest matching artifact row already has a
    drive_doc_id stamped on it. None when the artifact hasn't been
    pushed yet."""
    with engine.begin() as conn:
        row = conn.execute(
            select(
                meeting_prep_artifacts.c.drive_doc_id,
                meeting_prep_artifacts.c.drive_doc_url,
            ).where(
                meeting_prep_artifacts.c.partner_id == partner_id,
                meeting_prep_artifacts.c.artifact_type == artifact_type,
                meeting_prep_artifacts.c.signal_set_hash == signal_set_hash,
            ).order_by(desc(meeting_prep_artifacts.c.artifact_id)).limit(1)
        ).first()
    if row is None or not row.drive_doc_id:
        return None
    return _ExistingDoc(doc_id=row.drive_doc_id, doc_url=row.drive_doc_url)


def _stamp_drive_columns(
    engine: Engine, *, partner_id: str, artifact_type: str,
    signal_set_hash: str, doc_id: str, doc_url: str,
) -> None:
    """Write back the Drive identifiers onto the latest matching
    artifact row. We update the latest row only -- the cache always
    looks at the latest by artifact_id desc, so older rows for
    earlier signal sets stay as historical audit records without
    their own Drive links."""
    with engine.begin() as conn:
        # Find the latest artifact_id matching the key, then UPDATE
        # by primary key. SQLite doesn't support ORDER BY/LIMIT on
        # UPDATE so we split the lookup.
        row = conn.execute(
            select(meeting_prep_artifacts.c.artifact_id).where(
                meeting_prep_artifacts.c.partner_id == partner_id,
                meeting_prep_artifacts.c.artifact_type == artifact_type,
                meeting_prep_artifacts.c.signal_set_hash == signal_set_hash,
            ).order_by(desc(meeting_prep_artifacts.c.artifact_id)).limit(1)
        ).first()
        if row is None:
            return
        conn.execute(
            update(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.artifact_id == row.artifact_id
            ).values(
                drive_doc_id=doc_id,
                drive_doc_url=doc_url,
                drive_pushed_at=datetime.now(timezone.utc),
            )
        )
