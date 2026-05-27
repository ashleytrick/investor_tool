"""FR-4b: auto-stop hooks for the sequence state machine.

Covers:
  - core.sequences.auto_stop_sequence_if_active
    * stops an active sequence + records reason
    * no-op on already-stopped sequence (first reason wins)
    * no-op when partner has no sequence
    * respects cadence_settings.auto_stop_on_reply / pipeline /
      manual / fund_news toggles
    * default-true seeding when no cadence_settings row exists

  - reconcile_drafts_for_workspace auto-stops on reply
  - poll_crm_pipeline_for_workspace auto-stops on stage advance
  - hook response surfaces sequences_stopped counters
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


# ---------- fixtures ----------

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


def _seed_partner_with_active_sequence(
    engine, *, partner_id: str = "p_jane",
) -> str:
    """Insert fund + partner + active sequence. Returns sequence_id."""
    import secrets
    from core.db import funds, partners, sequences
    now = _dt.datetime.now(_dt.timezone.utc)
    seq_id = "seq_" + secrets.token_hex(8)
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="acme.com", name="Acme", domain="acme.com",
            is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id=partner_id, fund_id="acme.com", name="Jane",
            email=f"{partner_id}@acme.com",
        ))
        conn.execute(sequences.insert().values(
            sequence_id=seq_id, partner_id=partner_id,
            state="active", current_touch=1,
            created_at=now, updated_at=now,
        ))
    return seq_id


def _seed_cadence_settings(engine, **overrides) -> None:
    """Write a cadence_settings row with explicit toggles."""
    from core.db import cadence_settings, upsert
    defaults = {
        "key": "default",
        "enabled": True,
        "paused": False,
        "max_touches": 4,
        "daily_mix_new_pct": 60,
        "auto_stop_on_reply": True,
        "auto_stop_on_pipeline_advance": True,
        "auto_stop_on_manual_pass": True,
        "auto_stop_on_fund_news": False,
        "updated_at": _dt.datetime.now(_dt.timezone.utc),
    }
    defaults.update(overrides)
    with engine.begin() as conn:
        upsert(conn, cadence_settings, ["key"], defaults)


def _read_sequence(engine, seq_id: str):
    from sqlalchemy import select
    from core.db import sequences
    with engine.begin() as conn:
        return conn.execute(
            select(sequences).where(sequences.c.sequence_id == seq_id)
        ).first()


# ---------- helper: auto_stop_sequence_if_active ----------

def test_auto_stop_flips_active_sequence(engine) -> None:
    seq_id = _seed_partner_with_active_sequence(engine)
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="reply",
        )
    assert stopped is True
    row = _read_sequence(engine, seq_id)
    assert row.state == "stopped"
    assert row.stopped_reason == "reply"


def test_auto_stop_is_idempotent_on_already_stopped(engine) -> None:
    """Re-running the helper on a stopped sequence does NOT
    overwrite the original stopped_reason -- the first stop wins,
    matching /sequences/{id}/stop's contract."""
    seq_id = _seed_partner_with_active_sequence(engine)
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="reply",
        )
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="pipeline",
        )
    assert stopped is False
    row = _read_sequence(engine, seq_id)
    # First reason ('reply') stuck.
    assert row.stopped_reason == "reply"


def test_auto_stop_noop_when_partner_has_no_sequence(engine) -> None:
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_ghost", reason="reply",
        )
    assert stopped is False


def test_auto_stop_respects_disabled_reply_toggle(engine) -> None:
    _seed_partner_with_active_sequence(engine)
    _seed_cadence_settings(engine, auto_stop_on_reply=False)
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="reply",
        )
    assert stopped is False


def test_auto_stop_respects_disabled_pipeline_toggle(engine) -> None:
    _seed_partner_with_active_sequence(engine)
    _seed_cadence_settings(engine, auto_stop_on_pipeline_advance=False)
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="pipeline",
        )
    assert stopped is False


def test_auto_stop_fund_news_disabled_by_default(engine) -> None:
    """When no cadence_settings row exists, auto_stop_on_fund_news
    defaults to False (matches the seed in web/routers/cadence)."""
    _seed_partner_with_active_sequence(engine)
    # No cadence_settings row.
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="fund_news",
        )
    assert stopped is False


def test_auto_stop_reply_enabled_by_default(engine) -> None:
    """Reply auto-stop defaults to True even when cadence_settings
    is empty."""
    _seed_partner_with_active_sequence(engine)
    from core.sequences import auto_stop_sequence_if_active
    with engine.begin() as conn:
        stopped = auto_stop_sequence_if_active(
            conn, partner_id="p_jane", reason="reply",
        )
    assert stopped is True


# ---------- reconcile_drafts_for_workspace ----------

def test_reconcile_drafts_auto_stops_on_reply(workspace, engine) -> None:
    seq_id = _seed_partner_with_active_sequence(engine)
    # Insert a reply event for the partner.
    from core.db import outreach_events
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(outreach_events.insert().values(
            source="gmail", event_type="replied",
            external_id="<reply-1@gmail.com>",
            thread_id="thread-1", partner_id="p_jane",
            occurred_at=now, unread=True, created_at=now,
        ))

    from core.outreach_events import reconcile_drafts_for_workspace
    ws = MagicMock()
    ws.path = workspace
    result = reconcile_drafts_for_workspace(ws)
    assert result.error is None
    assert result.unread_replies == 1
    assert result.sequences_stopped == 1
    # The sequence is now stopped with reason='reply'.
    row = _read_sequence(engine, seq_id)
    assert row.state == "stopped"
    assert row.stopped_reason == "reply"


def test_reconcile_drafts_ignores_replies_predating_sequence(
    workspace, engine,
) -> None:
    """P1 audit fix: a partner who emailed the operator months
    BEFORE being captured should NOT have their freshly-seeded
    sequence auto-stopped. The reconcile pass must only consider
    reply events whose occurred_at >= the sequence's created_at."""
    seq_id = _seed_partner_with_active_sequence(engine)
    # Old reply: arrived a month before the sequence was created.
    from core.db import outreach_events, sequences
    from sqlalchemy import select
    with engine.begin() as conn:
        seq_created = conn.execute(
            select(sequences.c.created_at).where(
                sequences.c.sequence_id == seq_id,
            )
        ).scalar()
    old_reply = seq_created - _dt.timedelta(days=30)
    with engine.begin() as conn:
        conn.execute(outreach_events.insert().values(
            source="gmail", event_type="replied",
            external_id="<old-reply@gmail.com>",
            thread_id="thread-old", partner_id="p_jane",
            occurred_at=old_reply, unread=False,
            created_at=old_reply,
        ))
    from core.outreach_events import reconcile_drafts_for_workspace
    ws = MagicMock()
    ws.path = workspace
    result = reconcile_drafts_for_workspace(ws)
    # The old reply must NOT auto-stop the sequence.
    assert result.sequences_stopped == 0
    row = _read_sequence(engine, seq_id)
    assert row.state == "active"


def test_reconcile_drafts_skips_when_reply_auto_stop_disabled(
    workspace, engine,
) -> None:
    seq_id = _seed_partner_with_active_sequence(engine)
    _seed_cadence_settings(engine, auto_stop_on_reply=False)
    from core.db import outreach_events
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(outreach_events.insert().values(
            source="gmail", event_type="replied",
            external_id="<reply-x@gmail.com>",
            thread_id="thread-x", partner_id="p_jane",
            occurred_at=now, unread=True, created_at=now,
        ))
    from core.outreach_events import reconcile_drafts_for_workspace
    ws = MagicMock()
    ws.path = workspace
    result = reconcile_drafts_for_workspace(ws)
    assert result.sequences_stopped == 0
    row = _read_sequence(engine, seq_id)
    assert row.state == "active"


def test_reconcile_drafts_dedupes_repeat_replies_for_same_partner(
    workspace, engine,
) -> None:
    """Two reply events on the same partner produce one auto-stop
    (the helper is idempotent)."""
    seq_id = _seed_partner_with_active_sequence(engine)
    from core.db import outreach_events
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        for i in range(3):
            conn.execute(outreach_events.insert().values(
                source="gmail", event_type="replied",
                external_id=f"<reply-{i}@gmail.com>",
                thread_id=f"thread-{i}", partner_id="p_jane",
                occurred_at=now, unread=True, created_at=now,
            ))
    from core.outreach_events import reconcile_drafts_for_workspace
    ws = MagicMock()
    ws.path = workspace
    result = reconcile_drafts_for_workspace(ws)
    # All three are unread, but only one stop fires.
    assert result.unread_replies == 3
    assert result.sequences_stopped == 1
    row = _read_sequence(engine, seq_id)
    assert row.state == "stopped"


# ---------- poll_crm_pipeline_for_workspace ----------

def _seed_crm_connection(engine) -> None:
    """Encrypt a dummy api_key and write a crm_connections row so
    poll_crm_pipeline_for_workspace finds a provider to talk to."""
    from cryptography.fernet import Fernet
    import os
    key = Fernet.generate_key()
    os.environ["CRM_ENCRYPTION_KEY"] = key.decode()
    encrypted = Fernet(key).encrypt(b"dummy-attio-key").decode()
    from core.db import crm_connections
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(crm_connections.insert().values(
            provider="attio", encrypted_api_key=encrypted,
            key_suffix="-key", connected_at=now,
        ))


def test_poll_crm_pipeline_auto_stops_on_stage_advance(
    workspace, engine, monkeypatch,
) -> None:
    seq_id = _seed_partner_with_active_sequence(engine)
    _seed_crm_connection(engine)

    # Fake CRM client that reports one pipeline update for our partner.
    fake_client = MagicMock()
    fake_client.list_pipeline_updates_since.return_value = [
        {
            "partner_email": "p_jane@acme.com",
            "stage": "meeting_set",
            "notes": "intro call booked",
            "updated_at": _dt.datetime.now(_dt.timezone.utc),
        }
    ]

    from core.crm_polling import poll_crm_pipeline_for_workspace
    ws = MagicMock()
    ws.path = workspace
    results = poll_crm_pipeline_for_workspace(
        ws, client_factory=lambda *_args, **_kw: fake_client,
    )
    assert len(results) == 1
    assert results[0].error is None
    assert results[0].inserted == 1

    row = _read_sequence(engine, seq_id)
    assert row.state == "stopped"
    assert row.stopped_reason == "pipeline"


def test_poll_crm_pipeline_skips_when_stage_unchanged(
    workspace, engine,
) -> None:
    """Audit-review fix: list_pipeline_updates_since uses a 30-day
    lookback and re-yields existing stage rows. The auto-stop
    should only fire when the stage actually changed, otherwise a
    just-captured sequence gets killed on the first poll because
    the partner already has a CRM stage on file."""
    from core.db import partner_pipeline as _partner_pipeline
    seq_id = _seed_partner_with_active_sequence(engine)
    _seed_crm_connection(engine)
    # Pre-existing pipeline row at stage='lead'.
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(_partner_pipeline.insert().values(
            partner_id="p_jane", stage="lead", notes=None,
            updated_at=now, updated_by="crm:attio",
        ))
    fake_client = MagicMock()
    # Same stage as already on file -- no advance.
    fake_client.list_pipeline_updates_since.return_value = [
        {
            "partner_email": "p_jane@acme.com",
            "stage": "lead",
            "notes": None,
            "updated_at": now,
        }
    ]
    from core.crm_polling import poll_crm_pipeline_for_workspace
    ws = MagicMock()
    ws.path = workspace
    poll_crm_pipeline_for_workspace(
        ws, client_factory=lambda *_a, **_kw: fake_client,
    )
    row = _read_sequence(engine, seq_id)
    assert row.state == "active", (
        "auto-stop must NOT fire when the CRM stage didn't change"
    )


def test_poll_crm_pipeline_skips_when_disabled(
    workspace, engine,
) -> None:
    seq_id = _seed_partner_with_active_sequence(engine)
    _seed_crm_connection(engine)
    _seed_cadence_settings(engine, auto_stop_on_pipeline_advance=False)

    fake_client = MagicMock()
    fake_client.list_pipeline_updates_since.return_value = [
        {
            "partner_email": "p_jane@acme.com",
            "stage": "researching",
            "notes": None,
            "updated_at": _dt.datetime.now(_dt.timezone.utc),
        }
    ]

    from core.crm_polling import poll_crm_pipeline_for_workspace
    ws = MagicMock()
    ws.path = workspace
    poll_crm_pipeline_for_workspace(
        ws, client_factory=lambda *_args, **_kw: fake_client,
    )
    row = _read_sequence(engine, seq_id)
    assert row.state == "active"


# ---------- hook surface: ReconcileDraftsResult ----------

def test_reconcile_hook_surfaces_total_sequences_stopped(
    workspace, engine, monkeypatch,
) -> None:
    """The /api/public/hooks/reconcile-drafts response includes
    total_sequences_stopped + per-tenant sequences_stopped so the
    operator can see auto-stop activity in the hook log."""
    _seed_partner_with_active_sequence(engine)
    from core.db import outreach_events
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(outreach_events.insert().values(
            source="gmail", event_type="replied",
            external_id="<reply-hook@gmail.com>",
            thread_id="thread-hook", partner_id="p_jane",
            occurred_at=now, unread=True, created_at=now,
        ))

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
        "/api/public/hooks/reconcile-drafts",
        headers={"X-Hook-Secret": "hook-secret-xyz"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_sequences_stopped"] == 1
    assert body["results"][0]["sequences_stopped"] == 1
