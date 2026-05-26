"""Review item #19: LLM-assisted reply classifier.

Pre-this-fix: `classify_reply` was a pure heuristic; ambiguous
replies fell to 'unclear' even when a model could have given a
useful answer.

Post-this-fix: heuristic runs first (cheap, deterministic). If
heuristic returns 'unclear' AND a live LLM is available, fall
through to a Claude call. Model-side errors fall back to
'unclear'. Stub mode (no API key) keeps heuristic-only behavior.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------- heuristic path (untouched) ----------

def test_heuristic_still_short_circuits_obvious_cases() -> None:
    """LLM is irrelevant when the heuristic resolves -- no LLM
    call should ever fire for an obvious reply."""
    from core.outreach_events import classify_reply
    llm_called = {"n": 0}

    class _FakeLLM:
        def complete_json(self, **kwargs):
            llm_called["n"] += 1
            raise AssertionError("LLM should not be called")

    assert classify_reply(
        "Tell me more.", llm=_FakeLLM(),
    ) == "interested"
    assert classify_reply(
        "Not a fit.", llm=_FakeLLM(),
    ) == "pass"
    assert classify_reply(
        "Sure, here's calendly.com/me", llm=_FakeLLM(),
    ) == "meeting_booked"
    assert llm_called["n"] == 0


def test_heuristic_unclear_without_llm_stays_unclear() -> None:
    from core.outreach_events import classify_reply
    assert classify_reply("Got it. Thanks.") == "unclear"
    assert classify_reply("Got it. Thanks.", llm=None) == "unclear"


# ---------- LLM-assisted path ----------

def test_llm_resolves_ambiguous_reply_to_interested() -> None:
    """Heuristic says unclear; LLM upgrades to 'interested'."""
    from core.outreach_events import classify_reply

    class _FakeLLM:
        def complete_json(self, *, prompt, schema, max_tokens, stub_response):
            return schema.model_validate({"label": "interested"})

    body = "Got it. Could you share the deck?"
    assert classify_reply(body, llm=_FakeLLM()) == "interested"


def test_llm_resolves_ambiguous_reply_to_pass() -> None:
    from core.outreach_events import classify_reply

    class _FakeLLM:
        def complete_json(self, **kwargs):
            return kwargs["schema"].model_validate({"label": "pass"})

    assert classify_reply(
        "I appreciate the note but we're focused elsewhere.",
        llm=_FakeLLM(),
    ) == "pass"


def test_llm_unexpected_label_falls_back_to_unclear() -> None:
    """If the model returns something off-vocab (e.g. 'maybe'),
    the classifier must not propagate -- we collapse to 'unclear'
    so downstream callers can trust the four-label contract."""
    from core.outreach_events import classify_reply

    class _FakeLLM:
        def complete_json(self, **kwargs):
            return kwargs["schema"].model_validate({"label": "maybe"})

    assert classify_reply(
        "Hard to say.", llm=_FakeLLM(),
    ) == "unclear"


def test_llm_exception_falls_back_to_unclear() -> None:
    """Network blip / rate limit / etc. must not crash the poll
    pass. The classifier swallows the exception and returns the
    heuristic's fallback."""
    from core.outreach_events import classify_reply

    class _ExplodingLLM:
        def complete_json(self, **kwargs):
            raise RuntimeError("simulated 5xx")

    assert classify_reply(
        "Got it. Will revert.", llm=_ExplodingLLM(),
    ) == "unclear"


# ---------- integration: poll_gmail_replies uses the LLM ----------

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


def _seed_sent_event(workspace_path: Path) -> None:
    import datetime as _dt
    from core.db import (
        email_drafts, get_engine, outreach_events, partners,
    )
    eng = get_engine(f"sqlite:///{workspace_path}/data/pipeline.db")
    with eng.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id="p_x", name="P", email="p@x.example",
        ))
        conn.execute(outreach_events.insert().values(
            source="gmail",
            event_type="sent",
            external_id="<orig@gmail.com>",
            thread_id="t-1",
            occurred_at=_dt.datetime(
                2026, 5, 26, 12, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            recipient_email="p@x.example",
            partner_id="p_x",
            unread=False,
            created_at=_dt.datetime.now(_dt.timezone.utc),
        ))


def test_poll_replies_uses_llm_for_ambiguous_reply(workspace: Path) -> None:
    import datetime as _dt
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.db import get_engine, outreach_events
    from core.outreach_events import poll_gmail_replies_for_workspace
    from sqlalchemy import select

    fake_reply = {
        "external_id": "<r1@gmail.com>",
        "thread_id": "t-1",
        "occurred_at": _dt.datetime(
            2026, 5, 27, 9, 0, 0, tzinfo=_dt.timezone.utc,
        ),
        "recipient_email": "p@x.example",
        "subject": "Re: intro",
        # Snippet that the heuristic won't resolve. The fake LLM
        # will say "interested".
        "body_snippet": "Got the note. Could you walk me through the metrics page?",
        "unread": True,
    }

    def gmail_factory(_ws):
        c = MagicMock()
        c.list_replies_since.return_value = [fake_reply]
        return c

    class _FakeLLM:
        def complete_json(self, **kwargs):
            return kwargs["schema"].model_validate({"label": "interested"})

    def llm_factory(_ws):
        return _FakeLLM()

    ws = load_workspace(str(workspace))
    r = poll_gmail_replies_for_workspace(
        ws,
        gmail_client_factory=gmail_factory,
        llm_factory=llm_factory,
    )
    assert r.inserted == 1
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    with eng.begin() as conn:
        row = conn.execute(
            select(outreach_events).where(
                outreach_events.c.event_type == "replied",
            )
        ).first()
    assert row.classification == "interested"


def test_poll_replies_works_in_stub_llm_mode(workspace: Path) -> None:
    """No ANTHROPIC_API_KEY -> LLMClient is in stub mode. The
    classifier path stays heuristic-only; nothing 5xx's."""
    import datetime as _dt
    _seed_sent_event(workspace)
    from core.config_loader import load_workspace
    from core.outreach_events import poll_gmail_replies_for_workspace

    def gmail_factory(_ws):
        c = MagicMock()
        c.list_replies_since.return_value = [{
            "external_id": "<r2@gmail.com>",
            "thread_id": "t-1",
            "occurred_at": _dt.datetime(
                2026, 5, 27, 9, 0, 0, tzinfo=_dt.timezone.utc,
            ),
            "recipient_email": "p@x.example",
            "subject": "Re: intro",
            "body_snippet": "Tell me more.",  # heuristic catches this
            "unread": True,
        }]
        return c

    # Don't pass llm_factory -> the default builds an LLMClient
    # from the workspace which is in stub mode without an API key.
    ws = load_workspace(str(workspace))
    r = poll_gmail_replies_for_workspace(
        ws, gmail_client_factory=gmail_factory,
    )
    assert r.inserted == 1
    assert r.error is None
