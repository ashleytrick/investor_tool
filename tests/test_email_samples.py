"""Tests for the email-samples upload + voice-mirroring endpoints.

Covers:
  - GET /settings/email-samples
  - POST /settings/email-samples (validation + per-workspace cap)
  - DELETE /settings/email-samples/{id}
  - load_voice_samples_for_prompt() picks N most-recent + formats them
  - Stage 7 prompt receives the OPERATOR_VOICE_SAMPLES block
"""
from __future__ import annotations

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


_GOOD_BODY = (
    "Hey Sam -- noticed your recent post on infra startups. "
    "We're building Acme, the compliance API for fintechs; "
    "raising a seed round to ship self-serve onboarding for the "
    "three largest reporting regimes. 4 design partners, $200K ARR. "
    "Happy to share a one-pager if useful. -- Jane"
)


# ---------- POST /settings/email-samples ----------

def test_add_sample_returns_stored_row(client) -> None:
    res = client.post(
        "/settings/email-samples",
        json={"body": _GOOD_BODY, "subject": "intro to Sam"},
        headers=_auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["body"] == _GOOD_BODY.strip()
    assert body["subject"] == "intro to Sam"
    assert body["sample_id"] > 0


def test_add_sample_rejects_too_short_body(client) -> None:
    res = client.post(
        "/settings/email-samples",
        json={"body": "hi"},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_add_sample_rejects_too_long_body(client) -> None:
    res = client.post(
        "/settings/email-samples",
        json={"body": "a" * 10_001},
        headers=_auth(),
    )
    assert res.status_code == 422


def test_add_sample_caps_at_ten_per_workspace(client) -> None:
    """Per-workspace cap protects against the operator dumping a
    huge corpus that would bloat the Stage 7 prompt."""
    for i in range(10):
        res = client.post(
            "/settings/email-samples",
            json={"body": _GOOD_BODY + f" #{i}"},
            headers=_auth(),
        )
        assert res.status_code == 200
    # 11th should 409.
    res = client.post(
        "/settings/email-samples",
        json={"body": _GOOD_BODY + " #11"},
        headers=_auth(),
    )
    assert res.status_code == 409
    assert "cap" in res.text.lower()


def test_add_sample_requires_auth(client) -> None:
    res = client.post(
        "/settings/email-samples", json={"body": _GOOD_BODY},
    )
    assert res.status_code == 401


# ---------- GET /settings/email-samples ----------

def test_list_samples_returns_newest_first(client) -> None:
    """The prompt loader takes the N most-recent; the list
    endpoint orders the same way so the UI shows them in the
    order they'd actually influence drafts."""
    for i in range(3):
        client.post(
            "/settings/email-samples",
            json={"body": _GOOD_BODY + f" v{i}"},
            headers=_auth(),
        )
    res = client.get("/settings/email-samples", headers=_auth())
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 3
    # Newest first -> the v2 sample we just added is on top.
    assert "v2" in rows[0]["body"]
    assert "v0" in rows[2]["body"]


def test_list_samples_empty_workspace_returns_empty_list(client) -> None:
    res = client.get("/settings/email-samples", headers=_auth())
    assert res.status_code == 200
    assert res.json() == []


# ---------- DELETE /settings/email-samples/{id} ----------

def test_delete_sample(client) -> None:
    add = client.post(
        "/settings/email-samples",
        json={"body": _GOOD_BODY},
        headers=_auth(),
    )
    sid = add.json()["sample_id"]
    res = client.delete(
        f"/settings/email-samples/{sid}", headers=_auth(),
    )
    assert res.status_code == 200
    # Gone from list.
    assert client.get(
        "/settings/email-samples", headers=_auth(),
    ).json() == []


def test_delete_sample_404_for_unknown_id(client) -> None:
    res = client.delete(
        "/settings/email-samples/99999", headers=_auth(),
    )
    assert res.status_code == 404


# ---------- load_voice_samples_for_prompt (Stage 7 helper) ----------

def test_load_voice_samples_returns_empty_when_none(workspace) -> None:
    """Empty workspace -> "" so Stage 7's prompt picks up the
    fallback "no operator-uploaded samples yet" hint."""
    from core.db import get_engine
    from web.routers.email_samples import load_voice_samples_for_prompt
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    assert load_voice_samples_for_prompt(eng) == ""


def test_load_voice_samples_caps_at_three_most_recent(client, workspace) -> None:
    """Even if the operator uploaded 10 samples, the prompt block
    only contains the 3 most recent -- bounded token cost."""
    for i in range(5):
        client.post(
            "/settings/email-samples",
            json={
                "body": _GOOD_BODY + f" sample-marker-{i}",
                "subject": f"sub-{i}",
            },
            headers=_auth(),
        )
    from core.db import get_engine
    from web.routers.email_samples import load_voice_samples_for_prompt
    eng = get_engine(f"sqlite:///{workspace}/data/pipeline.db")
    block = load_voice_samples_for_prompt(eng)
    assert block, "block should not be empty"
    # 3 most-recent markers present (sample-marker-2/3/4), oldest two absent.
    assert "sample-marker-4" in block
    assert "sample-marker-3" in block
    assert "sample-marker-2" in block
    assert "sample-marker-0" not in block
    assert "sample-marker-1" not in block
    # Header includes the subject for operator readability.
    assert "sub-4" in block


# ---------- prompt-substitution wiring ----------

def test_prompt_substitutes_operator_voice_samples_block() -> None:
    """build_live_prompt's new operator_voice_samples kwarg gets
    substituted into {OPERATOR_VOICE_SAMPLES}. Empty default
    expands to a fallback hint, not the literal placeholder."""
    from core.email.prompt import build_live_prompt
    tmpl = (
        "Founder voice:\n"
        "- Style: {FOUNDER_VOICE_STYLE}\n"
        "- Banned: {FOUNDER_BANNED_PHRASES}\n"
        "\n"
        "Operator voice samples:\n"
        "{OPERATOR_VOICE_SAMPLES}\n"
        "\n"
        "{TOP_SIGNALS} {COMPOSITE_SCORE} {ROUND_FIT_SCORE} "
        "{LEAD_LIKELIHOOD_SCORE} {TOP_AXES_NAMES_AND_SCORES} "
        "{ADJACENT_PORTFOLIO_COMPANIES} {RECENT_PARTNER_LED_DEALS} "
        "{COMM_STYLE} {KILL_SIGNALS} {EXAMPLES_BLOCK} {EXAMPLES_DIR} "
        "{MEETING_DURATION} {MEETING_FORMAT} {SCHEDULING_LINK} "
        "{TIME_1} {TIME_2} {ROUND_FIT_REASONING} {PARTNER_NAME} "
        "{FUND_NAME} {PARTNER_BIO}"
    )
    out = build_live_prompt(
        prompt_template=tmpl,
        company_cfg={
            "company": {"name": "Acme", "founder_name": "Jane"},
            "raise_context": {},
            "founder_voice": {"style": "x", "banned_phrases": []},
        },
        partner_name="Sam",
        fund_name="Acme",
        partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/nowhere",
        operator_voice_samples="--- sample 1 ---\nhello world",
    )
    assert "--- sample 1 ---" in out
    assert "hello world" in out
    assert "{OPERATOR_VOICE_SAMPLES}" not in out


def test_prompt_empty_voice_samples_uses_fallback_hint() -> None:
    """When the operator hasn't uploaded anything, the prompt
    block expands to a short hint rather than the literal
    placeholder (which would confuse the LLM)."""
    from core.email.prompt import build_live_prompt
    tmpl = (
        "Founder voice:\n- Style: {FOUNDER_VOICE_STYLE}\n"
        "- Banned: {FOUNDER_BANNED_PHRASES}\n"
        "Operator voice samples:\n{OPERATOR_VOICE_SAMPLES}\n"
        "{TOP_SIGNALS} {COMPOSITE_SCORE} {ROUND_FIT_SCORE} "
        "{LEAD_LIKELIHOOD_SCORE} {TOP_AXES_NAMES_AND_SCORES} "
        "{ADJACENT_PORTFOLIO_COMPANIES} {RECENT_PARTNER_LED_DEALS} "
        "{COMM_STYLE} {KILL_SIGNALS} {EXAMPLES_BLOCK} {EXAMPLES_DIR} "
        "{MEETING_DURATION} {MEETING_FORMAT} {SCHEDULING_LINK} "
        "{TIME_1} {TIME_2} {ROUND_FIT_REASONING} {PARTNER_NAME} "
        "{FUND_NAME} {PARTNER_BIO}"
    )
    out = build_live_prompt(
        prompt_template=tmpl,
        company_cfg={
            "company": {"name": "Acme", "founder_name": "Jane"},
            "raise_context": {},
            "founder_voice": {"style": "x", "banned_phrases": []},
        },
        partner_name="Sam", fund_name="Acme", partner_bio=None,
        composite_score=None, round_fit_score=None,
        round_fit_reasoning=None, lead_likelihood_score=None,
        axes_summary=None, fund_kill_signals=None,
        signals_for_partner=[], deals_for_partner=[],
        examples_dir="/nowhere",
        # Default: no samples uploaded.
    )
    assert "{OPERATOR_VOICE_SAMPLES}" not in out, (
        "fallback must replace the literal placeholder"
    )
    assert "no operator-uploaded samples yet" in out
