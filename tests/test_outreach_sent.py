"""B2: tests for the Gmail Sent poll + GET /sent.

Gmail is fully mocked -- no test hits the network. The poll layer
is tested directly (`poll_gmail_sent_for_workspace`) AND through
the FastAPI hook (`POST /api/public/hooks/poll-gmail-sent`).
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------- fixtures ----------

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Fresh fixture workspace copy with the DB initialized (so
    `outreach_events` exists)."""
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "test_workspace"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    # Touch the engine once so metadata.create_all builds the schema.
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


def _fake_msg(
    external_id: str = "<msg-1@gmail.com>",
    *,
    recipient: str = "partner@example.com",
    subject: str = "Quick intro",
    snippet: str = "Hi -- saw your recent investment...",
    occurred_at: _dt.datetime | None = None,
    thread_id: str = "thread-1",
) -> dict:
    return {
        "external_id": external_id,
        "thread_id": thread_id,
        "occurred_at": occurred_at or _dt.datetime(
            2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
        ),
        "recipient_email": recipient,
        "subject": subject,
        "body_snippet": snippet,
    }


def _fake_client_factory(messages: list[dict]):
    """Returns a callable that produces a Gmail-client stub
    matching the polling layer's expectations."""
    def _factory(ws):
        client = MagicMock()
        client.list_sent_since.return_value = list(messages)
        return client
    return _factory


# ---------- core.outreach_events.poll_gmail_sent_for_workspace ----------

def test_poll_inserts_new_events(workspace: Path) -> None:
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace
    ws = load_workspace(str(workspace))
    result = poll_gmail_sent_for_workspace(
        ws, gmail_client_factory=_fake_client_factory([_fake_msg()]),
    )
    assert result.inserted == 1
    assert result.error is None


def test_poll_is_idempotent_on_repeated_message_ids(workspace: Path) -> None:
    """Re-polling the same Gmail message must NOT duplicate the
    event row (UNIQUE on source, external_id)."""
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace
    ws = load_workspace(str(workspace))
    factory = _fake_client_factory([_fake_msg()])
    poll_gmail_sent_for_workspace(ws, gmail_client_factory=factory)
    result = poll_gmail_sent_for_workspace(ws, gmail_client_factory=factory)
    assert result.inserted == 0


def test_poll_skips_silently_when_no_gmail_token(workspace: Path) -> None:
    """A workspace that never connected Gmail returns inserted=0
    with no error -- this is steady state for fresh tenants."""
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace

    def factory_missing(_ws):
        raise FileNotFoundError("no .gmail_token.json")

    ws = load_workspace(str(workspace))
    result = poll_gmail_sent_for_workspace(
        ws, gmail_client_factory=factory_missing,
    )
    assert result.inserted == 0
    assert result.error is None


def test_poll_catches_gmail_list_errors_per_workspace(workspace: Path) -> None:
    """A 5xx from Gmail must NOT explode the poll pass -- it lands
    as a per-workspace `error` so the hook caller can alert."""
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace

    def factory_exploding(_ws):
        client = MagicMock()
        client.list_sent_since.side_effect = RuntimeError(
            "gmail server 503"
        )
        return client

    ws = load_workspace(str(workspace))
    result = poll_gmail_sent_for_workspace(
        ws, gmail_client_factory=factory_exploding,
    )
    assert result.inserted == 0
    assert result.error is not None
    assert "gmail_list_failed" in result.error


def test_poll_matches_recipient_to_partner_id(workspace: Path) -> None:
    """When the recipient email matches a partner row (case-
    insensitive), the event carries that partner_id."""
    from core.config_loader import load_workspace
    from core.db import get_engine, partners
    from core.outreach_events import poll_gmail_sent_for_workspace

    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with engine.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_test", name="Test Partner",
            email="Partner@Example.com",  # mixed case on purpose
        ))

    ws = load_workspace(str(workspace))
    poll_gmail_sent_for_workspace(
        ws,
        gmail_client_factory=_fake_client_factory([
            _fake_msg(recipient="partner@example.com"),
        ]),
    )

    from core.db import outreach_events
    from sqlalchemy import select
    with engine.begin() as conn:
        row = conn.execute(
            select(outreach_events.c.partner_id)
            .where(outreach_events.c.external_id == "<msg-1@gmail.com>")
        ).first()
    assert row is not None
    assert row.partner_id == "p_test"


def test_high_water_mark_advances_between_polls(workspace: Path) -> None:
    """latest_event_at() returns the MAX(occurred_at) so the next
    poll can resume from there."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.outreach_events import (
        latest_event_at, poll_gmail_sent_for_workspace,
    )

    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    assert latest_event_at(
        engine, source="gmail", event_type="sent",
    ) is None

    ws = load_workspace(str(workspace))
    msg = _fake_msg(occurred_at=_dt.datetime(
        2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
    ))
    poll_gmail_sent_for_workspace(
        ws, gmail_client_factory=_fake_client_factory([msg]),
    )
    hwm = latest_event_at(
        engine, source="gmail", event_type="sent",
    )
    assert hwm is not None
    # SQLite may strip tzinfo; compare naive UTC.
    assert hwm.replace(tzinfo=None) == msg["occurred_at"].replace(tzinfo=None)


# ---------- FastAPI endpoints ----------

def _client_for_workspace(workspace: Path, monkeypatch, *, hook_secret: str = "s3"):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("HOOK_SECRET", hook_secret)
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def test_get_sent_returns_empty_list_when_no_events(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.get("/sent", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == []


def test_get_sent_requires_auth(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    assert client.get("/sent").status_code == 401


def test_get_sent_returns_inserted_events(workspace, monkeypatch) -> None:
    # Seed an event via the poll layer, then read via /sent.
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace
    ws = load_workspace(str(workspace))
    poll_gmail_sent_for_workspace(
        ws, gmail_client_factory=_fake_client_factory([_fake_msg()]),
    )

    client = _client_for_workspace(workspace, monkeypatch)
    res = client.get("/sent", headers=_auth_headers())
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    item = items[0]
    assert item["external_id"] == "<msg-1@gmail.com>"
    assert item["recipient_email"] == "partner@example.com"
    assert item["subject"] == "Quick intro"
    assert item["thread_id"] == "thread-1"
    # ISO datetime
    assert item["occurred_at"].startswith("2026-05-26")


def test_hook_requires_secret(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.post("/api/public/hooks/poll-gmail-sent")
    assert res.status_code == 401


def test_hook_rejects_wrong_secret(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.post(
        "/api/public/hooks/poll-gmail-sent",
        headers={"X-Hook-Secret": "wrong"},
    )
    assert res.status_code == 401


def test_hook_returns_401_when_secret_env_unset(workspace, monkeypatch) -> None:
    """Post-#5-fixup: missing HOOK_SECRET returns 401 (not 500) so
    an unauthenticated prober can't fingerprint operator
    misconfiguration via the differential status code. The
    operator still notices via a warning log line."""
    monkeypatch.delenv("HOOK_SECRET", raising=False)
    client = _client_for_workspace(workspace, monkeypatch, hook_secret="")
    monkeypatch.delenv("HOOK_SECRET", raising=False)  # ensure unset
    res = client.post(
        "/api/public/hooks/poll-gmail-sent",
        headers={"X-Hook-Secret": "anything"},
    )
    # 401 fail-closed; matches the wrong-secret path. Both 401 means
    # the prober can't distinguish env-unset from wrong-key.
    assert res.status_code == 401


def test_hook_polls_single_workspace_in_legacy_mode(workspace, monkeypatch) -> None:
    """When WORKSPACE_PER_USER is off, the hook polls the single
    configured workspace (legacy single-tenant deployments)."""
    # Patch the poller to a fake that returns 3 inserts.
    from core.outreach_events import PollResult as _PR

    def fake_poll(ws, gmail_client_factory=None):
        return _PR(workspace=str(ws.path), inserted=3, error=None)

    client = _client_for_workspace(workspace, monkeypatch)
    with patch("core.outreach_events.poll_gmail_sent_for_workspace", fake_poll):
        res = client.post(
            "/api/public/hooks/poll-gmail-sent",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["polled"] == 1
    assert body["total_inserted"] == 3
    assert body["results"][0]["inserted"] == 3


def test_hook_aggregates_per_tenant_errors(workspace, monkeypatch) -> None:
    """Per-tenant errors land in `results[].error`; the overall
    response is still 200 (the hook caller alerts on errors)."""
    from core.outreach_events import PollResult as _PR

    def fake_poll(ws, gmail_client_factory=None):
        return _PR(
            workspace=str(ws.path), inserted=0,
            error="gmail_list_failed: 503",
        )

    client = _client_for_workspace(workspace, monkeypatch)
    with patch("core.outreach_events.poll_gmail_sent_for_workspace", fake_poll):
        res = client.post(
            "/api/public/hooks/poll-gmail-sent",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total_inserted"] == 0
    assert "503" in (body["results"][0]["error"] or "")
