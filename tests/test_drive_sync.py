"""Tests for Build Session 13 -- Drive auto-push for meeting prep.

DriveClient is mocked end-to-end; no test hits real Google Drive.
The integration is covered by:
- filename convention (stable across re-runs against same signal set)
- skip when Drive scope not granted on the saved token
- skip when artifact already pushed (idempotency)
- happy-path push stamps drive_doc_id + drive_doc_url + drive_pushed_at
- prep_brief.py surfaces a "Drive sync" footer the operator can read
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import REPO_ROOT, run_script


# --- pure unit: filename --------------------------------------------------

def test_filename_is_stable_for_same_inputs() -> None:
    """Same (partner, signal_set, type, date) -> same filename --
    that's the idempotency guarantee that lets the auto-push
    convention work without uploading duplicates each run."""
    from core.meeting_prep.drive_sync import filename_for_artifact
    today = datetime(2026, 5, 26, tzinfo=timezone.utc)
    a = filename_for_artifact(
        partner_id="p_jane", signal_set_hash="deadbeef" * 8,
        artifact_type="objection_map", today=today,
    )
    b = filename_for_artifact(
        partner_id="p_jane", signal_set_hash="deadbeef" * 8,
        artifact_type="objection_map", today=today,
    )
    assert a == b
    assert "p_jane" in a
    assert "objection_map" in a
    assert "2026-05-26" in a
    assert "deadbeef" in a  # first 8 chars of the hash


def test_filename_changes_when_signal_set_changes() -> None:
    """A new signal flips the hash, which flips the filename. Without
    that, the operator's Drive would silently mask out-of-date briefs
    behind a 'same name, same content' assumption."""
    from core.meeting_prep.drive_sync import filename_for_artifact
    today = datetime(2026, 5, 26, tzinfo=timezone.utc)
    a = filename_for_artifact(
        partner_id="p_jane", signal_set_hash="a" * 64,
        artifact_type="objection_map", today=today,
    )
    b = filename_for_artifact(
        partner_id="p_jane", signal_set_hash="b" * 64,
        artifact_type="objection_map", today=today,
    )
    assert a != b


# --- push_if_needed branches ---------------------------------------------

@dataclass
class _FakeDriveClient:
    """Stand-in for core.drive_client.DriveClient. Records calls so
    the test can assert which (filename, body) the integration tried
    to upload."""
    uploads: list[tuple[str, str]]

    def upload_brief(self, *, filename: str, markdown_text: str):
        self.uploads.append((filename, markdown_text))
        return f"doc_id_{len(self.uploads)}", f"https://docs.example/{filename}"


def test_push_skipped_when_drive_scope_missing(workspace: Path) -> None:
    """No OAuth token = drive_connected False = push is a no-op.
    Returns a structured 'skipped' result so the caller can render
    the reason rather than silently dropping the brief."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.drive_sync import push_if_needed

    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)
    res = push_if_needed(
        engine, ws,
        partner_id="any", signal_set_hash="x" * 64,
        artifact_type="objection_map",
        markdown_text="# brief\n",
    )
    assert res.pushed is False
    assert res.doc_id is None
    assert "drive scope" in (res.skipped_reason or "").lower()


def test_push_happy_path_stamps_drive_columns(workspace: Path) -> None:
    """When the Drive scope is present, push_if_needed uploads via
    the client and writes drive_doc_id / drive_doc_url onto the
    matching meeting_prep_artifacts row so subsequent runs don't
    re-upload the same signal set."""
    from core.config_loader import load_workspace
    from core.db import (
        get_engine,
        meeting_prep_artifacts,
        partners,
    )
    from core.meeting_prep.drive_sync import push_if_needed

    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)

    # Seed a partner row + an artifact row so push_if_needed has
    # something to stamp. We don't need a full pipeline run.
    with engine.begin() as conn:
        # Partner FK is required for the artifact row.
        from core.db import funds
        conn.execute(funds.insert().values(
            fund_id="fund_x", name="X", domain="x.example",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(partners.insert().values(
            partner_id="p_jane", name="Jane", fund_id="fund_x",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(meeting_prep_artifacts.insert().values(
            partner_id="p_jane",
            artifact_type="objection_map",
            signal_set_hash="a" * 64,
            payload_json="{}",
            insufficient_evidence=False,
            generated_at=datetime.now(timezone.utc),
        ))

    fake_client = _FakeDriveClient(uploads=[])
    # Bypass the drive_connected gate by patching it for this test.
    with patch(
        "core.meeting_prep.drive_sync.drive_connected", return_value=True,
    ):
        res = push_if_needed(
            engine, ws,
            partner_id="p_jane", signal_set_hash="a" * 64,
            artifact_type="objection_map",
            markdown_text="# brief\n",
            drive_client_factory=lambda: fake_client,
        )

    assert res.pushed is True
    assert res.doc_id == "doc_id_1"
    assert res.doc_url.startswith("https://docs.example/")

    # The artifact row got stamped.
    with engine.begin() as conn:
        from sqlalchemy import select
        row = conn.execute(
            select(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.partner_id == "p_jane",
            )
        ).first()
    assert row.drive_doc_id == "doc_id_1"
    assert row.drive_doc_url is not None
    assert row.drive_pushed_at is not None


def test_push_idempotent_when_doc_id_already_stamped(workspace: Path) -> None:
    """Second push with the same signal_set_hash must hit zero Drive
    API calls -- the stamped doc_id short-circuits the upload."""
    from core.config_loader import load_workspace
    from core.db import (
        funds,
        get_engine,
        meeting_prep_artifacts,
        partners,
    )
    from core.meeting_prep.drive_sync import push_if_needed

    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)

    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="fund_x", name="X", domain="x.example",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(partners.insert().values(
            partner_id="p_jane", name="Jane", fund_id="fund_x",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(meeting_prep_artifacts.insert().values(
            partner_id="p_jane",
            artifact_type="objection_map",
            signal_set_hash="a" * 64,
            payload_json="{}",
            insufficient_evidence=False,
            generated_at=datetime.now(timezone.utc),
            drive_doc_id="EXISTING_DOC",
            drive_doc_url="https://docs.example/existing",
            drive_pushed_at=datetime.now(timezone.utc),
        ))

    fake_client = _FakeDriveClient(uploads=[])
    with patch(
        "core.meeting_prep.drive_sync.drive_connected", return_value=True,
    ):
        res = push_if_needed(
            engine, ws,
            partner_id="p_jane", signal_set_hash="a" * 64,
            artifact_type="objection_map",
            markdown_text="# brief\n",
            drive_client_factory=lambda: fake_client,
        )

    assert res.pushed is False
    assert res.doc_id == "EXISTING_DOC"
    assert res.skipped_reason == "already pushed for this signal_set_hash"
    assert fake_client.uploads == [], (
        "idempotent re-run must not call the Drive API"
    )


def test_push_surfaces_upload_failure_without_raising(workspace: Path) -> None:
    """A failed upload is annoying but not fatal -- the local brief
    is still useful. The footer surfaces the reason so the operator
    can act (re-consent, network, etc.)."""
    from core.config_loader import load_workspace
    from core.db import (
        funds,
        get_engine,
        meeting_prep_artifacts,
        partners,
    )
    from core.drive_client import DriveError
    from core.meeting_prep.drive_sync import push_if_needed

    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)

    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="fund_x", name="X", domain="x.example",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(partners.insert().values(
            partner_id="p_jane", name="Jane", fund_id="fund_x",
            last_updated=datetime.now(timezone.utc),
        ))
        conn.execute(meeting_prep_artifacts.insert().values(
            partner_id="p_jane",
            artifact_type="objection_map",
            signal_set_hash="a" * 64,
            payload_json="{}",
            insufficient_evidence=False,
            generated_at=datetime.now(timezone.utc),
        ))

    class _FailingClient:
        def upload_brief(self, **_kwargs):
            raise DriveError("quota exhausted")

    with patch(
        "core.meeting_prep.drive_sync.drive_connected", return_value=True,
    ):
        res = push_if_needed(
            engine, ws,
            partner_id="p_jane", signal_set_hash="a" * 64,
            artifact_type="objection_map",
            markdown_text="# brief\n",
            drive_client_factory=lambda: _FailingClient(),
        )

    assert res.pushed is False
    assert res.doc_id is None
    assert "quota exhausted" in (res.skipped_reason or "")


# --- prep_brief.py integration --------------------------------------------

def _seed_outcome(db: Path, *, partner_id: str, outreach_status: str) -> None:
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO outcomes (partner_id, outreach_status, source) "
        "VALUES (?, ?, 'fixture')",
        (partner_id, outreach_status),
    )
    c.commit()
    c.close()


def test_prep_brief_renders_drive_sync_footer_when_scope_missing(
    scored_workspace: Path,
) -> None:
    """End-to-end smoke: prep_brief.py must surface the Drive sync
    status even when the operator hasn't connected Google -- silent
    skips would hide the missing config from the operator."""
    pid = "northbeam.example_priya_anand"
    _seed_outcome(
        scored_workspace / "data" / "pipeline.db",
        partner_id=pid, outreach_status="meeting_booked",
    )
    res = run_script(
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", pid,
        cwd=REPO_ROOT,
    )
    # The footer surfaces the dossier push attempt with its skip
    # reason. (Build Session 16 collapsed objection_map +
    # framing_brief into the dossier; only one artifact remains.)
    assert "## Drive sync" in res.stdout
    assert "investor_dossier: skipped" in res.stdout
    assert "drive scope" in res.stdout.lower()


def test_prep_brief_no_drive_push_flag_suppresses_footer(
    scored_workspace: Path,
) -> None:
    """--no-drive-push opts out of the auto-push entirely; no Drive
    section appears in the brief."""
    pid = "northbeam.example_priya_anand"
    _seed_outcome(
        scored_workspace / "data" / "pipeline.db",
        partner_id=pid, outreach_status="meeting_booked",
    )
    res = run_script(
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", pid, "--no-drive-push",
        cwd=REPO_ROOT,
    )
    assert "## Drive sync" not in res.stdout
