"""Review items #14 (snoozes filter /today) + #18 (link draft_id
on Gmail sent events).

#14: a snoozed draft must disappear from `/today`, both when picks
are first materialized AND when an existing pick set is read back
after a snooze landed.

#18: the Gmail sent poller must populate
`outreach_events.draft_id` from the latest live draft for the
recipient's partner, not leave it NULL.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------- workspace fixture (uses cached scored ws + Stage 7) ----------

@pytest.fixture
def workspace_with_drafts(tmp_path: Path, _scored_workspace_source: Path) -> Path:
    from tests.conftest import _run
    dst = tmp_path / "test_workspace"
    shutil.copytree(_scored_workspace_source, dst)
    _run(
        "07_generate_emails.py", "--workspace", str(dst),
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )
    return dst


@pytest.fixture
def client(workspace_with_drafts: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace_with_drafts))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


def _future_iso(hours: int = 24) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    ).isoformat()


# ---------- #14: snoozes filter /today ----------

def test_today_excludes_snoozed_drafts_on_first_materialization(client) -> None:
    """A draft that's snoozed at materialization time should not
    appear in the freshly-built today_picks set."""
    # Snooze one draft.
    pending = client.get("/review/pending", headers=_auth()).json()
    if not pending:
        pytest.skip("fixture has no pending drafts")
    victim = pending[0]
    res = client.post(
        f"/snoozes/{victim['draft_id']}",
        json={"snoozed_until": _future_iso(48)},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text

    today = client.get("/today", headers=_auth()).json()
    assert all(t["draft_id"] != victim["draft_id"] for t in today), (
        "snoozed draft leaked into Today's picks"
    )


def test_today_excludes_snoozed_drafts_added_after_picks_materialize(
    client,
) -> None:
    """Build the pick set first, THEN snooze one of the picks.
    Reading /today again should drop the row even though it's
    still in today_picks."""
    today_before = client.get("/today", headers=_auth()).json()
    if not today_before:
        pytest.skip("no Today picks materialized")
    victim = today_before[0]
    client.post(
        f"/snoozes/{victim['draft_id']}",
        json={"snoozed_until": _future_iso(48)},
        headers=_auth(),
    )
    today_after = client.get("/today", headers=_auth()).json()
    assert all(t["draft_id"] != victim["draft_id"] for t in today_after)


def test_clearing_snooze_brings_draft_back_to_today(client) -> None:
    today_before = client.get("/today", headers=_auth()).json()
    if not today_before:
        pytest.skip("no Today picks materialized")
    victim = today_before[0]
    client.post(
        f"/snoozes/{victim['draft_id']}",
        json={"snoozed_until": _future_iso(48)},
        headers=_auth(),
    )
    # Snoozed -> filtered out.
    after_snooze = client.get("/today", headers=_auth()).json()
    assert all(t["draft_id"] != victim["draft_id"] for t in after_snooze)
    # Clear snooze.
    client.delete(f"/snoozes/{victim['draft_id']}", headers=_auth())
    after_clear = client.get("/today", headers=_auth()).json()
    assert any(t["draft_id"] == victim["draft_id"] for t in after_clear)


def test_past_dated_snooze_does_not_filter(client) -> None:
    """A snooze whose snoozed_until is in the past (i.e. elapsed)
    must NOT filter the draft -- the snooze has expired."""
    # We can't POST a past snooze (the endpoint rejects it), so
    # write directly to the DB to simulate an elapsed snooze.
    import os
    today_before = client.get("/today", headers=_auth()).json()
    if not today_before:
        pytest.skip("no Today picks materialized")
    victim = today_before[0]
    from core.db import draft_snoozes, get_engine
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    with eng.begin() as conn:
        conn.execute(draft_snoozes.insert().values(
            draft_id=victim["draft_id"],
            snoozed_until=past,
            reason="elapsed",
            created_at=past,
        ))
    today_after = client.get("/today", headers=_auth()).json()
    assert any(t["draft_id"] == victim["draft_id"] for t in today_after)


# ---------- #18: link draft_id on Gmail sent events ----------

def test_poll_gmail_sent_links_draft_id_to_latest_live_draft(
    tmp_path: Path,
) -> None:
    """When the recipient_email matches a partner with a live
    (non-superseded) draft, the inserted outreach_events row
    carries that draft_id rather than NULL."""
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    from core.db import (
        email_drafts, get_engine, outreach_events, partners,
    )
    from sqlalchemy import select
    eng = get_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="P", email="p@example.com",
        ))
        # Two drafts for the same partner; only the higher draft_id
        # is live.
        conn.execute(email_drafts.insert().values(
            draft_id=1, partner_id="p_x",
            subject="old", body="old", approval_status="superseded",
            superseded_at=_dt.datetime.now(_dt.timezone.utc),
        ))
        conn.execute(email_drafts.insert().values(
            draft_id=2, partner_id="p_x",
            subject="new", body="new", approval_status="approved_to_send",
        ))

    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace

    def _factory(_ws):
        c = MagicMock()
        c.list_sent_since.return_value = [{
            "external_id": "<m1@gmail.com>",
            "thread_id": "thr-1",
            "occurred_at": _dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "recipient_email": "p@example.com",
            "subject": "new",
            "body_snippet": "...",
        }]
        return c

    ws = load_workspace(str(dst))
    r = poll_gmail_sent_for_workspace(ws, gmail_client_factory=_factory)
    assert r.inserted == 1
    with eng.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.external_id == "<m1@gmail.com>"
            )
        ).first()
    assert row.draft_id == 2  # the latest live draft


def test_poll_leaves_draft_id_null_when_no_partner_match(
    tmp_path: Path,
) -> None:
    """An outbound Gmail to someone who isn't a partner (e.g.
    the operator emailing a colleague) should NOT make up a
    draft_id."""
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()

    from core.db import get_engine, outreach_events
    from sqlalchemy import select
    eng = get_engine(f"sqlite:///{db}")  # schema init

    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_sent_for_workspace

    def _factory(_ws):
        c = MagicMock()
        c.list_sent_since.return_value = [{
            "external_id": "<m2@gmail.com>",
            "thread_id": "thr-2",
            "occurred_at": _dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "recipient_email": "stranger@example.com",
            "subject": "hi",
            "body_snippet": "...",
        }]
        return c

    ws = load_workspace(str(dst))
    poll_gmail_sent_for_workspace(ws, gmail_client_factory=_factory)
    with eng.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.external_id == "<m2@gmail.com>"
            )
        ).first()
    assert row.draft_id is None
    assert row.partner_id is None
