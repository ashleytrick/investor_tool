"""FR-4c: send_pace is a HARD daily cap on NEW outreach;
follow-ups roll over daily in a separate array.

Covers:
  - `sent_today` counts new-outreach gmail-sent events for today
  - `drafts` shrinks to (send_pace - sent_today)
  - When the operator hits send_pace, `drafts` is empty
  - `limit` query param can only LOWER the cap, never raise it
  - `follow_ups` is empty when no follow_up_drafts rows exist
  - `follow_ups` is populated + uncapped when rows exist
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
def workspace_with_drafts(tmp_path: Path, _scored_workspace_source: Path) -> Path:
    """Scored workspace + one Stage 7 run so we have reviewable
    drafts to play with."""
    from tests.conftest import _run  # noqa: PLC2701
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


def _stamp_sent(workspace: Path, *, draft_id: int, n: int = 1) -> None:
    """Insert n synthetic 'sent' outreach_events rows for today."""
    import os
    from core.db import get_engine, outreach_events
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    now = _dt.datetime.utcnow()
    with eng.begin() as conn:
        for i in range(n):
            conn.execute(outreach_events.insert().values(
                source="gmail", event_type="sent",
                external_id=f"<stamp-{draft_id}-{i}@gmail.com>",
                thread_id=f"thread-stamp-{i}",
                partner_id=None,
                draft_id=draft_id,
                occurred_at=now,
                unread=False,
                created_at=now,
            ))


# ---------- new envelope fields ----------

def test_envelope_has_sent_today_and_follow_ups_fields(client) -> None:
    """FR-4c shape: response carries sent_today (int) and
    follow_ups (list) in addition to the FR-4 envelope."""
    body = client.get("/today", headers=_auth()).json()
    assert "sent_today" in body
    assert "follow_ups" in body
    assert isinstance(body["sent_today"], int)
    assert isinstance(body["follow_ups"], list)


def test_sent_today_starts_at_zero_when_no_sends(client) -> None:
    body = client.get("/today", headers=_auth()).json()
    assert body["sent_today"] == 0


def test_sent_today_counts_today_sent_events(
    client, workspace_with_drafts: Path,
) -> None:
    """A gmail 'sent' event for today, linked to an email_drafts row,
    increments sent_today by 1."""
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("fixture didn't materialize any drafts")
    target_draft_id = body["drafts"][0]["draft_id"]
    _stamp_sent(workspace_with_drafts, draft_id=target_draft_id, n=2)
    body2 = client.get("/today", headers=_auth()).json()
    assert body2["sent_today"] == 2


# ---------- hard cap behavior ----------

def test_drafts_shrinks_as_sent_today_grows(
    client, workspace_with_drafts: Path,
) -> None:
    """send_pace=5, sent_today=0 -> drafts size <= 5. After 2
    sends today, drafts size <= 3 (5 - 2)."""
    client.post(
        "/settings/send-pace", json={"value": 5}, headers=_auth(),
    )
    body = client.get("/today", headers=_auth()).json()
    if len(body["drafts"]) < 3:
        pytest.skip("need >= 3 drafts in fixture to exercise shrinkage")
    initial_count = len(body["drafts"])
    target_draft_id = body["drafts"][0]["draft_id"]
    _stamp_sent(workspace_with_drafts, draft_id=target_draft_id, n=2)
    body2 = client.get("/today", headers=_auth()).json()
    assert body2["sent_today"] == 2
    # remaining_pace = 5 - 2 = 3
    assert len(body2["drafts"]) <= 3, (
        f"drafts must respect remaining pace (5-2=3); "
        f"got {len(body2['drafts'])} drafts when initial was {initial_count}"
    )


def test_drafts_empty_when_send_pace_reached(
    client, workspace_with_drafts: Path,
) -> None:
    """Operator hits the daily cap -> drafts becomes empty (no
    more NEW outreach to show today). next_drafts still works as
    a preview of tomorrow's queue."""
    client.post(
        "/settings/send-pace", json={"value": 1}, headers=_auth(),
    )
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("no drafts in fixture")
    target_draft_id = body["drafts"][0]["draft_id"]
    _stamp_sent(workspace_with_drafts, draft_id=target_draft_id, n=1)
    body2 = client.get("/today", headers=_auth()).json()
    assert body2["sent_today"] == 1
    assert body2["drafts"] == []  # cap hit
    # But next_drafts can still show the preview.
    assert "next_drafts" in body2


def test_limit_query_can_only_lower_not_raise_above_pace(
    client, workspace_with_drafts: Path,
) -> None:
    """A `limit=20` query param doesn't override the daily pace
    cap. Once sent_today reaches send_pace, no `limit` value gets
    more drafts."""
    client.post(
        "/settings/send-pace", json={"value": 2}, headers=_auth(),
    )
    body = client.get("/today", headers=_auth()).json()
    if not body["drafts"]:
        pytest.skip("no drafts in fixture")
    target_draft_id = body["drafts"][0]["draft_id"]
    _stamp_sent(workspace_with_drafts, draft_id=target_draft_id, n=2)
    body2 = client.get(
        "/today?limit=20", headers=_auth(),
    ).json()
    assert body2["drafts"] == [], (
        "limit must not override the hard daily cap"
    )


# ---------- follow_ups split ----------

def test_follow_ups_empty_when_no_follow_up_drafts(client) -> None:
    """No follow_up_drafts rows in the fixture -> follow_ups is
    [] regardless of send_pace state."""
    body = client.get("/today", headers=_auth()).json()
    assert body["follow_ups"] == []


def test_follow_ups_roll_over_uncapped(
    client, workspace_with_drafts: Path,
) -> None:
    """Seed a follow_up_drafts row for a partner with an active
    sequence and assert it appears in follow_ups -- regardless of
    send_pace state (follow-ups don't burn the daily budget)."""
    import os
    import secrets
    from core.db import (
        follow_up_drafts, get_engine, partners as _partners,
        sequences as _sequences, funds,
    )
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    now = _dt.datetime.utcnow()
    seq_id = "seq_" + secrets.token_hex(8)
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="rollover.com", name="Rollover Capital",
            domain="rollover.com", is_active=True,
        ))
        conn.execute(_partners.insert().values(
            partner_id="p_rollover", fund_id="rollover.com",
            name="Rita Rollover",
        ))
        conn.execute(_sequences.insert().values(
            sequence_id=seq_id, partner_id="p_rollover",
            state="active", current_touch=2,
            created_at=now - _dt.timedelta(days=5),
            updated_at=now - _dt.timedelta(days=5),
        ))
        conn.execute(follow_up_drafts.insert().values(
            sequence_id=seq_id, touch_number=2, angle="new_signal",
            why_now="recent fund news",
            body="Followup body here...",
            status="draft",
            created_at=now - _dt.timedelta(days=3),
        ))
    # Hit the send_pace cap first so we know follow-ups still surface.
    client.post(
        "/settings/send-pace", json={"value": 1}, headers=_auth(),
    )
    body0 = client.get("/today", headers=_auth()).json()
    if body0["drafts"]:
        _stamp_sent(workspace_with_drafts,
                    draft_id=body0["drafts"][0]["draft_id"], n=1)
    body = client.get("/today", headers=_auth()).json()
    # Cap is hit -> drafts empty.
    assert body["drafts"] == []
    # But follow_ups must still surface our seeded touch-2 row.
    assert len(body["follow_ups"]) == 1
    fu = body["follow_ups"][0]
    assert fu["partner_id"] == "p_rollover"
    assert fu["follow_up"]["touch_number"] == 2
    assert fu["follow_up"]["sequence_id"] == seq_id
    assert fu["follow_up"]["angle"] == "new_signal"
    assert fu["follow_up"]["days_since_last_touch"] == 3


def test_follow_ups_respect_next_touch_due_at_in_the_future(
    client, workspace_with_drafts: Path,
) -> None:
    """Audit-review fix: follow-up drafts whose sequences.next_touch_due_at
    is in the FUTURE should NOT surface in Today.follow_ups. The
    cadence gap is real; pre-fix the join filtered only by
    status+state and ignored due-at."""
    import os
    import secrets
    from core.db import (
        follow_up_drafts, funds, get_engine, partners as _partners,
        sequences as _sequences,
    )
    eng = get_engine(
        f"sqlite:///{os.environ['INVESTOR_WORKSPACE']}/data/pipeline.db"
    )
    now = _dt.datetime.utcnow()
    future = now + _dt.timedelta(days=5)
    seq_id = "seq_" + secrets.token_hex(8)
    with eng.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="future.com", name="Future Capital",
            domain="future.com", is_active=True,
        ))
        conn.execute(_partners.insert().values(
            partner_id="p_future", fund_id="future.com",
            name="Felix Future",
        ))
        conn.execute(_sequences.insert().values(
            sequence_id=seq_id, partner_id="p_future",
            state="active", current_touch=1,
            next_touch_due_at=future,
            created_at=now - _dt.timedelta(days=2),
            updated_at=now - _dt.timedelta(days=2),
        ))
        conn.execute(follow_up_drafts.insert().values(
            sequence_id=seq_id, touch_number=2, angle="new_signal",
            why_now="precomputed but not yet due",
            body="Followup body, scheduled for 5 days from now.",
            status="draft",
            created_at=now - _dt.timedelta(days=1),
        ))
    body = client.get("/today", headers=_auth()).json()
    assert not any(
        fu["partner_id"] == "p_future" for fu in body["follow_ups"]
    ), "future-due follow-up leaked into Today.follow_ups"


# ---------- auth ----------

def test_today_envelope_requires_auth(client) -> None:
    assert client.get("/today").status_code == 401
