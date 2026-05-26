"""B3: tests for Gmail reply polling + classification + reconcile.

Covers `poll_gmail_replies_for_workspace`, `classify_reply`,
`reconcile_drafts_for_workspace`, and the new endpoints
(`/api/public/hooks/poll-gmail-replies`, `/api/public/hooks/reconcile-drafts`,
`GET /replies`, `POST /replies/{event_id}/read`).

Gmail is fully mocked.
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
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "test_workspace"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


def _fake_reply(
    external_id: str = "<reply-1@gmail.com>",
    *,
    sender: str = "partner@example.com",
    thread_id: str = "thread-known",
    subject: str = "Re: Quick intro",
    snippet: str = "Tell me more.",
    occurred_at: _dt.datetime | None = None,
    unread: bool = True,
) -> dict:
    return {
        "external_id": external_id,
        "thread_id": thread_id,
        "occurred_at": occurred_at or _dt.datetime(
            2026, 5, 27, 9, 0, 0, tzinfo=_dt.timezone.utc,
        ),
        "recipient_email": sender,  # for replies, this carries the sender
        "subject": subject,
        "body_snippet": snippet,
        "unread": unread,
        "is_reply": True,
    }


def _fake_factory(messages: list[dict]):
    def _factory(_ws):
        c = MagicMock()
        c.list_replies_since.return_value = list(messages)
        return c
    return _factory


def _seed_sent_event(
    workspace: Path, *,
    partner_id: str | None = "p_partner",
    thread_id: str = "thread-known",
    recipient_email: str = "partner@example.com",
) -> None:
    """Insert a synthetic sent event so the reply poller has a
    thread to reconcile against."""
    from core.db import get_engine, outreach_events, partners
    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with engine.begin() as conn:
        if partner_id:
            conn.execute(partners.insert().values(
                partner_id=partner_id,
                name="Partner",
                email=recipient_email,
            ))
        conn.execute(outreach_events.insert().values(
            source="gmail",
            event_type="sent",
            external_id="<original-msg@gmail.com>",
            thread_id=thread_id,
            occurred_at=_dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            recipient_email=recipient_email,
            subject="Quick intro",
            body_snippet="Hi -- saw your recent investment...",
            partner_id=partner_id,
            draft_id=None,
            unread=False,
            created_at=_dt.datetime.now(_dt.timezone.utc),
        ))


# ---------- classify_reply ----------

def test_classify_reply_meeting_booked_wins_over_interested() -> None:
    from core.outreach_events import classify_reply
    text = "This is interesting -- happy to chat. https://calendly.com/foo"
    assert classify_reply(text) == "meeting_booked"


def test_classify_reply_interested() -> None:
    from core.outreach_events import classify_reply
    assert classify_reply("Tell me more.") == "interested"
    assert classify_reply("Would love to learn more.") == "interested"


def test_classify_reply_pass() -> None:
    from core.outreach_events import classify_reply
    assert classify_reply("Not a fit for us.") == "pass"
    assert classify_reply("Best of luck with the round.") == "pass"


def test_classify_reply_unclear_for_empty_or_neutral_text() -> None:
    from core.outreach_events import classify_reply
    assert classify_reply("") == "unclear"
    assert classify_reply(None) == "unclear"
    assert classify_reply("Got it. Thanks.") == "unclear"


# ---------- poll_gmail_replies_for_workspace ----------

def test_poll_replies_inserts_classified_event(workspace: Path) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.db import get_engine, outreach_events
    from core.outreach_events import poll_gmail_replies_for_workspace
    from sqlalchemy import select

    ws = load_workspace(str(workspace))
    result = poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([_fake_reply()]),
    )
    assert result.inserted == 1
    assert result.error is None

    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with engine.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.event_type == "replied",
            )
        ).first()
    assert row is not None
    assert row.classification == "interested"
    assert row.unread is True
    assert row.partner_id == "p_partner"


def test_poll_replies_attributes_to_partner_via_thread_id(
    workspace: Path,
) -> None:
    """A reply from `assistant@firm.com` (different email) on a
    known thread should still attribute to the original partner."""
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.db import get_engine, outreach_events
    from core.outreach_events import poll_gmail_replies_for_workspace
    from sqlalchemy import select

    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([
            _fake_reply(sender="assistant@firm.com"),
        ]),
    )
    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with engine.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.event_type == "replied",
            )
        ).first()
    assert row.partner_id == "p_partner"


def test_poll_replies_idempotent(workspace: Path) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace
    ws = load_workspace(str(workspace))
    factory = _fake_factory([_fake_reply()])
    poll_gmail_replies_for_workspace(ws, gmail_client_factory=factory)
    r2 = poll_gmail_replies_for_workspace(ws, gmail_client_factory=factory)
    assert r2.inserted == 0


def test_poll_replies_returns_zero_when_no_sent_threads(
    workspace: Path,
) -> None:
    """No sent events -> nothing to reconcile against. Quiet 0."""
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace
    ws = load_workspace(str(workspace))
    result = poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([_fake_reply()]),
    )
    assert result.inserted == 0
    assert result.error is None


def test_poll_replies_skips_silently_when_no_gmail_token(
    workspace: Path,
) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace

    def factory_missing(_ws):
        raise FileNotFoundError("no token")

    ws = load_workspace(str(workspace))
    result = poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=factory_missing,
    )
    assert result.inserted == 0
    assert result.error is None


# ---------- reconcile_drafts_for_workspace ----------

def test_reconcile_counts_unread_replies(workspace: Path) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import (
        poll_gmail_replies_for_workspace,
        reconcile_drafts_for_workspace,
    )
    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([
            _fake_reply(unread=True),
            _fake_reply(
                external_id="<reply-2@gmail.com>",
                unread=False,
            ),
        ]),
    )
    r = reconcile_drafts_for_workspace(ws)
    assert r.unread_replies == 1
    assert r.error is None


# ---------- mark_reply_read ----------

def test_mark_reply_read_clears_unread(workspace: Path) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.db import get_engine, outreach_events
    from core.outreach_events import (
        mark_reply_read, poll_gmail_replies_for_workspace,
    )
    from sqlalchemy import select
    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([_fake_reply()]),
    )
    engine = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with engine.begin() as conn:
        event_id = conn.execute(
            select(outreach_events.c.event_id).where(
                outreach_events.c.event_type == "replied",
            )
        ).scalar()
    assert mark_reply_read(engine, event_id=int(event_id)) is True
    # Calling again returns False (already read).
    assert mark_reply_read(engine, event_id=int(event_id)) is False


# ---------- FastAPI endpoints ----------

def _client_for_workspace(workspace: Path, monkeypatch, *, hook_secret="s3"):
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


def test_get_replies_empty_when_no_events(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.get("/replies", headers=_auth_headers())
    assert res.status_code == 200
    assert res.json() == []


def test_get_replies_requires_auth(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    assert client.get("/replies").status_code == 401


def test_get_replies_returns_classified(workspace, monkeypatch) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace
    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([
            _fake_reply(snippet="https://calendly.com/me/30min"),
        ]),
    )

    client = _client_for_workspace(workspace, monkeypatch)
    res = client.get("/replies", headers=_auth_headers())
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["classification"] == "meeting_booked"
    assert items[0]["unread"] is True
    assert items[0]["sender_email"] == "partner@example.com"


def test_get_replies_unread_only_filter(workspace, monkeypatch) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace
    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([
            _fake_reply(external_id="<r1@x>", unread=True),
            _fake_reply(external_id="<r2@x>", unread=False),
        ]),
    )
    client = _client_for_workspace(workspace, monkeypatch)
    all_replies = client.get("/replies", headers=_auth_headers()).json()
    unread = client.get(
        "/replies?unread_only=true", headers=_auth_headers(),
    ).json()
    assert len(all_replies) == 2
    assert len(unread) == 1


def test_mark_reply_read_endpoint(workspace, monkeypatch) -> None:
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace
    ws = load_workspace(str(workspace))
    poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=_fake_factory([_fake_reply()]),
    )

    client = _client_for_workspace(workspace, monkeypatch)
    items = client.get("/replies", headers=_auth_headers()).json()
    assert items[0]["unread"] is True
    event_id = items[0]["event_id"]
    res = client.post(
        f"/replies/{event_id}/read", headers=_auth_headers(),
    )
    assert res.status_code == 200, res.text
    # The second call 404s because nothing is unread anymore.
    res2 = client.post(
        f"/replies/{event_id}/read", headers=_auth_headers(),
    )
    assert res2.status_code == 404


def test_poll_replies_hook_requires_secret(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.post("/api/public/hooks/poll-gmail-replies")
    assert res.status_code == 401


def test_reconcile_hook_requires_secret(workspace, monkeypatch) -> None:
    client = _client_for_workspace(workspace, monkeypatch)
    res = client.post("/api/public/hooks/reconcile-drafts")
    assert res.status_code == 401


def test_poll_replies_hook_aggregates_per_tenant(workspace, monkeypatch) -> None:
    from core.outreach_events import PollResult as _PR

    def fake_poll(ws, gmail_client_factory=None):
        return _PR(workspace=str(ws.path), inserted=2)

    client = _client_for_workspace(workspace, monkeypatch)
    with patch(
        "core.outreach_events.poll_gmail_replies_for_workspace", fake_poll,
    ):
        res = client.post(
            "/api/public/hooks/poll-gmail-replies",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total_inserted"] == 2


def test_reconcile_hook_aggregates_per_tenant(workspace, monkeypatch) -> None:
    from core.outreach_events import ReconcileResult as _RR

    def fake_reconcile(ws):
        return _RR(workspace=str(ws.path), unread_replies=5)

    client = _client_for_workspace(workspace, monkeypatch)
    with patch(
        "core.outreach_events.reconcile_drafts_for_workspace",
        fake_reconcile,
    ):
        res = client.post(
            "/api/public/hooks/reconcile-drafts",
            headers={"X-Hook-Secret": "s3"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total_unread_replies"] == 5
