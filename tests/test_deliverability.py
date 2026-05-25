"""Unit + integration tests for Slice 9 deliverability guardrails."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6

from core.deliverability import (
    DEFAULT_DAILY_APPROVAL_CAP,
    GENERIC_LOCAL_PARTS,
    VERIFICATION_INVALID,
    VERIFICATION_RISKY,
    VERIFICATION_VALID,
    check_recipient_duplicates,
    daily_approval_count,
    enforce_daily_approval_cap,
    is_generic_or_role_email,
)


# ----- is_generic_or_role_email -----


def test_generic_local_parts_flagged() -> None:
    for local in ("info", "hello", "partners", "team"):
        assert is_generic_or_role_email(f"{local}@a.example") is True


def test_plus_tagged_generic_still_flagged() -> None:
    """info+vc@... is still a generic mailbox; the +tag doesn't
    rescue it."""
    assert is_generic_or_role_email("info+seed@a.example") is True
    assert is_generic_or_role_email("hello+founders@a.example") is True


def test_non_generic_local_part_allowed() -> None:
    assert is_generic_or_role_email("priya@northbeam.example") is False
    assert is_generic_or_role_email("dana.cole@tidewater.example") is False


def test_partial_match_does_not_count() -> None:
    """`myinfo@` is not a role mailbox; only whole-token matches."""
    assert is_generic_or_role_email("myinfo@a.example") is False
    assert is_generic_or_role_email("information@a.example") is False


def test_none_or_empty_or_invalid_returns_false() -> None:
    assert is_generic_or_role_email(None) is False
    assert is_generic_or_role_email("") is False
    assert is_generic_or_role_email("not_an_email") is False


def test_generic_local_parts_set_covers_common_roles() -> None:
    for required in ("info", "hello", "partners", "team", "contact"):
        assert required in GENERIC_LOCAL_PARTS


# ----- check_recipient_duplicates -----


def test_duplicate_recipient_detected(tmp_path: Path) -> None:
    from core.db import funds, get_engine, partners
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="F", domain="f.example", is_active=True,
        ))
        for pid, email in (
            ("a", "shared@x.example"),
            ("b", "shared@x.example"),
            ("c", "unique@y.example"),
        ):
            conn.execute(partners.insert().values(
                partner_id=pid, fund_id="f1", name=pid, email=email,
            ))
    # Partner 'a' shares with 'b'; 'c' is unique.
    dups_a = check_recipient_duplicates(
        engine, partner_id="a", email="shared@x.example",
    )
    assert dups_a == ["b"]
    dups_c = check_recipient_duplicates(
        engine, partner_id="c", email="unique@y.example",
    )
    assert dups_c == []


def test_duplicate_recipient_case_insensitive(tmp_path: Path) -> None:
    from core.db import funds, get_engine, partners
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="F", domain="f.example", is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="a", fund_id="f1", name="A",
            email="Priya@Northbeam.example",
        ))
        conn.execute(partners.insert().values(
            partner_id="b", fund_id="f1", name="B",
            email="priya@northbeam.example",
        ))
    assert check_recipient_duplicates(
        engine, partner_id="a", email="PRIYA@NORTHBEAM.EXAMPLE",
    ) == ["b"]


# ----- daily approval cap -----


@pytest.fixture
def cap_engine(tmp_path: Path):
    from core.db import (
        draft_approvals, email_drafts, funds, get_engine, partners,
    )
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="f1", name="F", domain="f.example", is_active=True,
        ))
        conn.execute(partners.insert().values(
            partner_id="p1", fund_id="f1", name="P",
        ))
        # Seed an email_drafts row so draft_approvals' FK is satisfied.
        conn.execute(email_drafts.insert().values(
            draft_id=1, partner_id="p1", batch_id="b1",
            subject="s", body="b", is_recommended=True,
            generated_at=datetime.now(timezone.utc),
        ))
    return engine


def test_daily_count_starts_at_zero(cap_engine) -> None:
    assert daily_approval_count(cap_engine) == 0


def test_daily_count_counts_today_approvals(cap_engine) -> None:
    from core.db import draft_approvals
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    with cap_engine.begin() as conn:
        for ts, ev in (
            (today, "approved_to_send"),
            (today, "approved_to_send"),
            (yesterday, "approved_to_send"),
            (today, "rejected"),  # different event type
        ):
            conn.execute(draft_approvals.insert().values(
                draft_id=1, partner_id="p1",
                event_type=ev, actor="t", at=ts,
            ))
    # 2 today's approvals; yesterday's + today's reject don't count.
    assert daily_approval_count(cap_engine) == 2


def test_configured_daily_cap_reads_from_company_yaml() -> None:
    """Finding 6: cap is configurable via
    company.yaml's `deliverability.daily_approval_cap` rather than
    hardcoded at the CLI."""
    from types import SimpleNamespace
    from core.deliverability import (
        DEFAULT_DAILY_APPROVAL_CAP, configured_daily_cap,
    )
    # No config -> default.
    ws = SimpleNamespace(company={})
    assert configured_daily_cap(ws) == DEFAULT_DAILY_APPROVAL_CAP
    # Explicit override.
    ws = SimpleNamespace(company={"deliverability": {"daily_approval_cap": 7}})
    assert configured_daily_cap(ws) == 7
    # Numeric-string override (yaml-shaped).
    ws = SimpleNamespace(company={"deliverability": {"daily_approval_cap": "12"}})
    assert configured_daily_cap(ws) == 12
    # Garbage value falls back to default rather than disabling the cap.
    for bad in (0, -1, "not-a-number", None):
        ws = SimpleNamespace(
            company={"deliverability": {"daily_approval_cap": bad}},
        )
        assert configured_daily_cap(ws) == DEFAULT_DAILY_APPROVAL_CAP


def test_enforce_cap_blocks_when_reached(cap_engine) -> None:
    from core.db import draft_approvals
    now = datetime.now(timezone.utc)
    with cap_engine.begin() as conn:
        for _ in range(3):
            conn.execute(draft_approvals.insert().values(
                draft_id=1, partner_id="p1",
                event_type="approved_to_send", actor="t", at=now,
            ))
    blocked, count = enforce_daily_approval_cap(cap_engine, cap=3)
    assert blocked is True
    assert count == 3
    blocked2, _ = enforce_daily_approval_cap(cap_engine, cap=10)
    assert blocked2 is False


# ----- integration: approve CLI rejects when cap reached -----


def test_approve_cli_refuses_when_daily_cap_reached(tmp_path: Path) -> None:
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    ws = str(ws_dst)
    _run_pipeline_through_stage_6(ws_dst)
    _run(
        "07_generate_emails.py", "--workspace", ws,
        "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
    )
    db = ws_dst / "data" / "pipeline.db"
    # Seed N approvals for today directly + give the candidate draft's
    # partner a valid email so the new approval gate (Finding 2)
    # doesn't refuse before the cap check fires.
    c = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(DEFAULT_DAILY_APPROVAL_CAP):
        c.execute(
            "insert into draft_approvals "
            "(draft_id, partner_id, event_type, actor, at, draft_hash, notes) "
            "values (1, 'p', 'approved_to_send', 'sys', ?, '', '')",
            (now,),
        )
    draft_id, pid = c.execute(
        "select draft_id, partner_id from email_drafts "
        "where is_recommended = 1 limit 1"
    ).fetchone()
    c.execute(
        "update partners set email='op@operator.com', "
        "email_verification_status='valid' where partner_id=?",
        (pid,),
    )
    c.commit()
    c.close()

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
         "--workspace", ws, "--draft-id", str(draft_id),
         "--allow-example-domains"],
        capture_output=True, text=True,
        env={**os.environ, "USER": "t"}, timeout=60,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cap reached" in res.stdout
    # --override-cap allows.
    res2 = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
         "--workspace", ws, "--draft-id", str(draft_id), "--override-cap",
         "--allow-example-domains"],
        capture_output=True, text=True,
        env={**os.environ, "USER": "t"}, timeout=60,
    )
    assert res2.returncode == 0, res2.stdout + res2.stderr


# ----- integration: routing blockers -----


def test_routing_blocks_generic_email() -> None:
    from core.email.draft_routing import (
        HINT_QA_FAILED, decide_draft_routing,
    )
    company = {
        "company": {
            "name": "T", "founder_name": "D",
            "founder_email": "d@cal.example",
            "meeting_ask": {
                "preferred_scheduling_link": "https://cal.example/d",
            },
        },
    }
    body = (
        "Raising a $3M Seed. 30 minutes? https://cal.example/d"
    )
    d = decide_draft_routing(
        rec_subject="s", rec_body=body, rec_template_smell="low",
        in_sim_failure_pair=False,
        pctx_recommendation_reasoning="ok",
        pctx_recommended_to_send=True,
        pctx_cold_reachability_score=7.5,
        pctx_partner_email="info@northbeam.example",  # generic
        pctx_do_not_contact=False,
        banned=[], company_cfg=company, allow_example_domains=True,
    )
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("generic / role mailbox" in b for b in d.blockers)


def test_routing_blocks_invalid_verification_status() -> None:
    from core.email.draft_routing import (
        HINT_QA_FAILED, decide_draft_routing,
    )
    company = {
        "company": {
            "name": "T", "founder_name": "D",
            "founder_email": "d@cal.example",
            "meeting_ask": {
                "preferred_scheduling_link": "https://cal.example/d",
            },
        },
    }
    body = (
        "Raising a $3M Seed. 30 minutes? https://cal.example/d"
    )
    d = decide_draft_routing(
        rec_subject="s", rec_body=body, rec_template_smell="low",
        in_sim_failure_pair=False,
        pctx_recommendation_reasoning="ok",
        pctx_recommended_to_send=True,
        pctx_cold_reachability_score=7.5,
        pctx_partner_email="priya@northbeam.example",
        pctx_do_not_contact=False,
        pctx_email_verification_status=VERIFICATION_INVALID,
        banned=[], company_cfg=company, allow_example_domains=True,
    )
    assert d.approval_status_hint == HINT_QA_FAILED
    assert any("verification status = invalid" in b for b in d.blockers)
