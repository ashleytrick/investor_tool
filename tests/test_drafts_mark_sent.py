"""FR-7: POST /drafts/{id}/mark-sent + DELETE for the manual-paste flow.

The operator pastes a draft into LinkedIn (or another off-Gmail
channel) and clicks "mark sent" in the UI. Backend logs an
outreach_events row + flips approval_status to 'sent' so the
draft drops out of the Today queue. DELETE reverses the action
when the operator mis-clicked.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


@pytest.fixture
def client(workspace: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def _seed_partner_and_draft(
    workspace: Path,
    *,
    approval_status: str = "approved_to_send",
) -> tuple[str, int]:
    """Insert a fund + partner + email_draft. Returns (partner_id,
    draft_id). The draft starts approved by default so the
    mark-sent transition is the clean approved_to_send -> sent
    edge."""
    from core.db import email_drafts, funds, get_engine, partners
    eng = get_engine(
        f"sqlite:///{workspace}/data/pipeline.db"
    )
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="markco.com", name="Mark Co", domain="markco.com",
            is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="p_mark", fund_id="markco.com", name="Mark P",
            email="mark@markco.com",
        ))
        result = conn.execute(email_drafts.insert().values(
            partner_id="p_mark",
            subject="quick intro",
            body="Hey Mark, ... pitch ...",
            approval_status=approval_status,
        ))
        draft_id = int(result.inserted_primary_key[0])
    return "p_mark", draft_id


# ---------- POST /drafts/{id}/mark-sent ----------

def test_mark_sent_default_channel_is_linkedin(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    res = client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={}, headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_id"] == draft_id
    assert body["channel"] == "linkedin"  # default
    assert body["event_id"] > 0
    assert body["sent_at"]


def test_mark_sent_logs_outreach_event_with_channel(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin", "note": "DM sent"},
        headers=_auth(),
    )
    # Inspect the DB directly.
    import os
    from sqlalchemy import select
    from core.db import get_engine, outreach_events
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        rows = list(conn.execute(
            select(outreach_events).where(
                outreach_events.c.draft_id == draft_id,
                outreach_events.c.source == "app",
            )
        ))
    assert len(rows) == 1
    assert rows[0].channel == "linkedin"
    assert rows[0].event_type == "sent"


def test_mark_sent_flips_approval_status_to_sent(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    import os
    from sqlalchemy import select
    from core.db import email_drafts, get_engine
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        status = conn.execute(
            select(email_drafts.c.approval_status).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).scalar()
    assert status == "sent"


def test_mark_sent_rejects_invalid_channel(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    res = client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "smoke-signal"},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_mark_sent_404_for_unknown_draft(client) -> None:
    res = client.post(
        "/drafts/999999/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    assert res.status_code == 404


def test_mark_sent_is_idempotent_on_re_call(client, workspace) -> None:
    """Double-click on the UI button shouldn't pile up event rows.
    Re-calling returns the same event_id."""
    _, draft_id = _seed_partner_and_draft(workspace)
    res1 = client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    res2 = client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    assert res1.status_code == 200 and res2.status_code == 200
    assert res1.json()["event_id"] == res2.json()["event_id"]


def test_mark_sent_works_on_needs_review_draft(client, workspace) -> None:
    """Operator may skip the formal approve step (they wrote in
    LinkedIn directly). Mark-sent should still succeed by
    bypassing the state-machine table for non-approved-to-send
    starting states."""
    _, draft_id = _seed_partner_and_draft(
        workspace, approval_status="needs_review",
    )
    res = client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    assert res.status_code == 200, res.text
    # Status should be 'sent' now.
    import os
    from sqlalchemy import select
    from core.db import email_drafts, get_engine
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        status = conn.execute(
            select(email_drafts.c.approval_status).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).scalar()
    assert status == "sent"


def test_mark_sent_requires_auth(client) -> None:
    res = client.post(
        "/drafts/1/mark-sent", json={"channel": "linkedin"},
    )
    assert res.status_code == 401


# ---------- DELETE /drafts/{id}/mark-sent ----------

def test_clear_sent_reverts_approval_status(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    res = client.delete(
        f"/drafts/{draft_id}/mark-sent", headers=_auth(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["draft_id"] == draft_id
    # Status reverted.
    import os
    from sqlalchemy import select
    from core.db import email_drafts, get_engine
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        status = conn.execute(
            select(email_drafts.c.approval_status).where(
                email_drafts.c.draft_id == draft_id,
            )
        ).scalar()
    assert status == "approved_to_send"


def test_clear_sent_removes_the_app_event(client, workspace) -> None:
    _, draft_id = _seed_partner_and_draft(workspace)
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    client.delete(
        f"/drafts/{draft_id}/mark-sent", headers=_auth(),
    )
    import os
    from sqlalchemy import select
    from core.db import get_engine, outreach_events
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    with eng.begin() as conn:
        rows = list(conn.execute(
            select(outreach_events).where(
                outreach_events.c.draft_id == draft_id,
                outreach_events.c.source == "app",
            )
        ))
    assert rows == []


def test_clear_sent_404_when_no_app_event_on_file(client, workspace) -> None:
    """No mark-sent ever happened -> 404. Gmail-poll-confirmed
    sends are NOT reversible from this endpoint."""
    _, draft_id = _seed_partner_and_draft(workspace)
    res = client.delete(
        f"/drafts/{draft_id}/mark-sent", headers=_auth(),
    )
    assert res.status_code == 404


def test_clear_sent_404_for_unknown_draft(client) -> None:
    res = client.delete(
        "/drafts/999999/mark-sent", headers=_auth(),
    )
    assert res.status_code == 404


def test_clear_sent_requires_auth(client) -> None:
    res = client.delete("/drafts/1/mark-sent")
    assert res.status_code == 401


# ---------- audit-review fixes (#9, #10) ----------

def test_mark_sent_writes_audit_row_on_non_approved_starting_state(
    client, workspace,
) -> None:
    """Audit-review fix #9: mark-sent on a needs_review draft
    must write a draft_approvals row. Pre-fix, the bypass path
    stamped email_drafts.approval_status directly without an
    audit record."""
    _, draft_id = _seed_partner_and_draft(
        workspace, approval_status="needs_review",
    )
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    from sqlalchemy import select
    from core.db import draft_approvals, get_engine
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        rows = list(conn.execute(
            select(
                draft_approvals.c.event_type,
                draft_approvals.c.actor,
                draft_approvals.c.notes,
            ).where(draft_approvals.c.draft_id == draft_id)
        ))
    sent_rows = [r for r in rows if r.event_type == "sent"]
    assert len(sent_rows) == 1, (
        f"expected exactly one 'sent' audit row; got {rows}"
    )
    assert sent_rows[0].actor == "ui:mark_sent"
    assert "channel=linkedin" in (sent_rows[0].notes or "")
    assert "needs_review" in (sent_rows[0].notes or "")


def test_clear_sent_audit_row_uses_at_send_hash(
    client, workspace,
) -> None:
    """Audit-review fix #10: clear-sent's reversal audit row must
    record the hash on file AT mark-sent time, not the current
    body. Edit between mark-sent and clear-sent and assert the
    reversal carries the original hash."""
    _, draft_id = _seed_partner_and_draft(workspace)
    from sqlalchemy import select
    from core.db import draft_approvals, email_drafts, get_engine
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(
            email_drafts.update()
            .where(email_drafts.c.draft_id == draft_id)
            .values(draft_hash="HASH-AT-SEND")
        )
    client.post(
        f"/drafts/{draft_id}/mark-sent",
        json={"channel": "linkedin"}, headers=_auth(),
    )
    # Operator edits the body, hash drifts.
    with eng.begin() as conn:
        conn.execute(
            email_drafts.update()
            .where(email_drafts.c.draft_id == draft_id)
            .values(
                body="edited after sending",
                draft_hash="HASH-AFTER-EDIT",
            )
        )
    client.delete(
        f"/drafts/{draft_id}/mark-sent", headers=_auth(),
    )
    with eng.begin() as conn:
        rev = conn.execute(
            select(draft_approvals.c.draft_hash).where(
                draft_approvals.c.draft_id == draft_id,
                draft_approvals.c.actor == "ui:clear_sent",
            )
        ).first()
    assert rev is not None
    assert rev.draft_hash == "HASH-AT-SEND", (
        f"clear-sent audit must use AT-SEND hash, not current; "
        f"got {rev.draft_hash!r}"
    )


# ---------- channel column on outreach_events ----------

def test_outreach_events_channel_column_defaults_to_email(workspace) -> None:
    """FR-7 schema: legacy gmail-poll inserts (which don't pass
    channel) get channel='email' by default."""
    from sqlalchemy import select
    from core.db import get_engine, outreach_events
    eng = get_engine(
        f"sqlite:///{workspace}/data/pipeline.db"
    )
    now = _dt.datetime.utcnow()
    with eng.begin() as conn:
        conn.execute(outreach_events.insert().values(
            source="gmail", event_type="sent",
            external_id="<test@gmail.com>",
            occurred_at=now, unread=False, created_at=now,
        ))
        row = conn.execute(
            select(outreach_events.c.channel).where(
                outreach_events.c.external_id == "<test@gmail.com>",
            )
        ).first()
    assert row.channel == "email"
