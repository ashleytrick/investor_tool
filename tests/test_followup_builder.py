"""FR-5: tests for the LLM follow-up draft generation pass.

Covers:
  - build_follow_ups_for_workspace generates a row when the
    sequence is due and within max_touches
  - skips when next touch would exceed max_touches
  - skips when next_touch_due_at is in the future
  - idempotent: re-running doesn't double-insert
  - skips when no cadence_touches row exists for the next position
  - persisted row carries angle + why_now from the LLM (stub)
  - POST /api/public/hooks/build-follow-ups invokes the builder
    across tenants
"""
from __future__ import annotations

import datetime as _dt
import secrets
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
def engine(workspace: Path):
    from core.db import get_engine
    return get_engine(f"sqlite:///{workspace}/data/pipeline.db")


def _seed_cadence(engine, *, max_touches: int = 4) -> None:
    """Standard preset, condensed: 3 touches with sensible gaps."""
    from core.db import cadence_settings, cadence_touches, upsert
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        upsert(conn, cadence_settings, ["key"], {
            "key": "default",
            "enabled": True, "paused": False,
            "max_touches": max_touches,
            "daily_mix_new_pct": 60,
            "auto_stop_on_reply": True,
            "auto_stop_on_pipeline_advance": True,
            "auto_stop_on_manual_pass": True,
            "auto_stop_on_fund_news": False,
            "updated_at": now,
        })
        for pos, gap, angle in (
            (2, 3, "new_signal"),
            (3, 7, "specific_ask"),
            (4, 14, "graceful_close"),
        ):
            upsert(conn, cadence_touches, ["position"], {
                "position": pos, "gap_days": gap, "angle": angle,
                "custom_prompt": None, "updated_at": now,
            })


def _seed_partner_with_sequence(
    engine,
    *,
    partner_id: str = "p_followup",
    current_touch: int = 1,
    next_due_offset_days: int = -1,  # default: due yesterday
    seed_sent_event: bool = True,
    sent_offset_days: int = -3,
) -> str:
    """Insert fund + partner + initial email_draft + active
    sequence. Returns sequence_id.

    `seed_sent_event=True` (default) also inserts an
    `outreach_events` 'sent' row for the partner so the builder's
    prior-send gate passes. Tests that want to exercise the
    no-prior-send skip path pass False.
    """
    from core.db import (
        email_drafts, funds, outreach_events, partners, sequences,
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    next_due = (now + _dt.timedelta(
        days=next_due_offset_days,
    )).replace(tzinfo=None)
    seq_id = "seq_" + secrets.token_hex(8)
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="followup.com", name="Followup VC",
            domain="followup.com", is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id=partner_id, fund_id="followup.com",
            name="Fern Followup", email=f"{partner_id}@followup.com",
        ))
        conn.execute(email_drafts.insert().values(
            partner_id=partner_id,
            subject="initial intro: round closing soon",
            body="Hey Fern, original outreach body here, citing a signal.",
            approval_status="sent",
        ))
        if seed_sent_event:
            sent_at = (now + _dt.timedelta(
                days=sent_offset_days,
            )).replace(tzinfo=None)
            conn.execute(outreach_events.insert().values(
                source="gmail", event_type="sent",
                external_id=f"<seed-{partner_id}@gmail.com>",
                partner_id=partner_id,
                occurred_at=sent_at,
                unread=False, created_at=sent_at,
            ))
        conn.execute(sequences.insert().values(
            sequence_id=seq_id, partner_id=partner_id,
            state="active", current_touch=current_touch,
            next_touch_due_at=next_due,
            created_at=now, updated_at=now,
        ))
    return seq_id


def _stub_ws_with_path(workspace: Path):
    ws = MagicMock()
    ws.path = workspace
    ws.company = {
        "company": {"name": "Acme", "founder_name": "Jane"},
        "raise_context": {"round": "seed", "amount": "$1.5M"},
        "founder_voice": {"style": "direct", "banned_phrases": []},
    }
    ws.env = MagicMock(return_value=None)  # stub LLM (no API key)
    return ws


# ---------- core: build_follow_ups_for_workspace ----------

def test_build_generates_followup_for_due_active_sequence(
    workspace, engine,
) -> None:
    _seed_cadence(engine)
    seq_id = _seed_partner_with_sequence(engine)
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.errors == [], result.errors
    assert result.generated == 1
    # Persisted row.
    from sqlalchemy import select
    from core.db import follow_up_drafts
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(follow_up_drafts).where(
                follow_up_drafts.c.sequence_id == seq_id,
            )
        ))
    assert len(rows) == 1
    row = rows[0]
    assert row.touch_number == 2  # current_touch=1 -> next=2
    assert row.angle == "new_signal"
    assert row.status == "draft"
    assert row.body  # stub body is populated
    assert row.why_now  # rationale persisted


def test_build_skips_when_due_at_is_future(workspace, engine) -> None:
    _seed_cadence(engine)
    _seed_partner_with_sequence(
        engine, next_due_offset_days=2,  # due 2 days from now
    )
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0


def test_build_skips_when_next_touch_exceeds_max_touches(
    workspace, engine,
) -> None:
    _seed_cadence(engine, max_touches=3)
    _seed_partner_with_sequence(
        engine, current_touch=3,  # next would be 4 > max=3
    )
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0
    assert result.skipped_done == 1


def test_build_is_idempotent_on_re_run(workspace, engine) -> None:
    """The daily cron may fire multiple times; re-running on a
    sequence that already has a follow_up_drafts row for that
    touch_number must not insert a duplicate."""
    _seed_cadence(engine)
    seq_id = _seed_partner_with_sequence(engine)
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result1 = build_follow_ups_for_workspace(ws)
    result2 = build_follow_ups_for_workspace(ws)
    assert result1.generated == 1
    assert result2.generated == 0
    assert result2.skipped_existing == 1
    from sqlalchemy import select, func
    from core.db import follow_up_drafts
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(follow_up_drafts)
            .where(follow_up_drafts.c.sequence_id == seq_id)
        ).scalar()
    assert n == 1


def test_build_skips_when_no_cadence_touch_for_position(
    workspace, engine,
) -> None:
    """Operator seeded cadence_settings but only positions 2 and 3,
    with max_touches=4. A sequence at current_touch=3 needing
    position=4 has no cadence row -> skipped (with audit
    counter)."""
    from core.db import cadence_settings, cadence_touches, upsert
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        upsert(conn, cadence_settings, ["key"], {
            "key": "default",
            "enabled": True, "paused": False,
            "max_touches": 4,
            "daily_mix_new_pct": 60,
            "auto_stop_on_reply": True,
            "auto_stop_on_pipeline_advance": True,
            "auto_stop_on_manual_pass": True,
            "auto_stop_on_fund_news": False,
            "updated_at": now,
        })
        upsert(conn, cadence_touches, ["position"], {
            "position": 2, "gap_days": 3, "angle": "new_signal",
            "custom_prompt": None, "updated_at": now,
        })
        # Note: no row for position=4.
    _seed_partner_with_sequence(engine, current_touch=3)
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0
    assert result.skipped_no_cadence == 1


def test_build_skips_stopped_sequence(workspace, engine) -> None:
    """A sequence in state='stopped' should never get a new
    follow-up regardless of due date."""
    _seed_cadence(engine)
    from core.db import sequences
    seq_id = _seed_partner_with_sequence(engine)
    with engine.begin() as conn:
        conn.execute(
            sequences.update()
            .where(sequences.c.sequence_id == seq_id)
            .values(state="stopped", stopped_reason="reply")
        )
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0


def test_build_skips_when_touch_1_not_yet_sent(workspace, engine) -> None:
    """Audit-review fix: a freshly captured sequence (no
    `outreach_events.sent` row for the partner) should NOT get a
    touch-2 follow-up generated -- touch 1 hasn't gone out yet.
    Pre-fix, `next_touch_due_at=NULL` was interpreted as 'due now'
    and a 'Following up on my note from 0 days ago' body was
    written on day 0."""
    _seed_cadence(engine)
    _seed_partner_with_sequence(engine, seed_sent_event=False)
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0
    assert result.skipped_no_prior_send == 1


def test_build_skips_when_implied_due_at_is_future(
    workspace, engine,
) -> None:
    """Audit-review fix: when next_touch_due_at is NULL but a
    prior-send event exists, the implied due-at is sent_at +
    cadence_touches[next_touch].gap_days. If that's in the
    future, skip."""
    _seed_cadence(engine)
    # Sent yesterday + gap_days=3 for position 2 -> due in 2 days.
    _seed_partner_with_sequence(
        engine, sent_offset_days=-1, next_due_offset_days=0,
    )
    # Clear the explicit next_touch_due_at so the implied path runs.
    from core.db import sequences
    with engine.begin() as conn:
        conn.execute(
            sequences.update().values(next_touch_due_at=None)
        )
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0


def test_build_returns_error_when_no_cadence_settings(
    workspace, engine,
) -> None:
    """Operator hasn't configured cadence yet (no cadence_settings
    row). Don't crash; surface as a structured error so the hook
    log shows it."""
    _seed_partner_with_sequence(engine)
    from core.followup_builder import build_follow_ups_for_workspace
    ws = _stub_ws_with_path(workspace)
    result = build_follow_ups_for_workspace(ws)
    assert result.generated == 0
    assert result.errors and any(
        "cadence_settings" in e for e in result.errors
    )


# ---------- hook endpoint ----------

def test_build_follow_ups_hook_invokes_builder(
    workspace, engine, monkeypatch,
) -> None:
    _seed_cadence(engine)
    _seed_partner_with_sequence(engine)

    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("HOOK_SECRET", "hook-secret-xyz")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    res = client.post(
        "/api/public/hooks/build-follow-ups",
        headers={"X-Hook-Secret": "hook-secret-xyz"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_generated"] == 1
    assert len(body["results"]) >= 1
    assert body["results"][0]["generated"] == 1


def test_build_follow_ups_hook_requires_secret(workspace, monkeypatch) -> None:
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("HOOK_SECRET", "hook-secret-xyz")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    # No X-Hook-Secret header.
    res = client.post("/api/public/hooks/build-follow-ups")
    assert res.status_code == 401


# ---------- integration with Today queue ----------

def test_generated_followup_surfaces_in_today_follow_ups(
    workspace, engine, monkeypatch,
) -> None:
    """End-to-end: build runs, follow_up_drafts populated,
    GET /today's `follow_ups` array now contains the generated
    row (FR-4c + FR-5 wired together)."""
    _seed_cadence(engine)
    _seed_partner_with_sequence(engine)
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("HOOK_SECRET", "hook-secret-xyz")
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    client = TestClient(api_mod.app)
    # Trigger the build.
    client.post(
        "/api/public/hooks/build-follow-ups",
        headers={"X-Hook-Secret": "hook-secret-xyz"},
    )
    # Read Today.
    body = client.get(
        "/today",
        headers={"Authorization": "Bearer test-api-key"},
    ).json()
    assert body["follow_ups"], (
        "follow_up_drafts row should surface in Today.follow_ups"
    )
    fu = body["follow_ups"][0]
    assert fu["follow_up"]["touch_number"] == 2
    assert fu["follow_up"]["angle"] == "new_signal"
    assert fu["follow_up"]["sequence_id"]
