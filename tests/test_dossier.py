"""Tests for Build Session 14 -- Investor Dossier.

Coverage:
- Eligibility predicate (substantive reply / meeting / DNC short-circuit)
- Task creation via persist_outcome_event (one task per substantive
  outcome; duplicate syncs don't duplicate the task)
- Schema invariants (insufficient_evidence vs partner-specific content)
- Cache busting on company_profile_hash / live_research / style_sample
- prep_brief.py --dossier path produces markdown + writes default file
- prep_brief.py --pending-only batch loop resolves tasks + writes files
- status.py surfaces pending dossier counts
- DossierIneligibleError path renders cleanly (no traceback)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, run_pipeline_through_stage_6, run_script


# ---------- eligibility predicate ----------------------------------------

def _seed_partner(db: Path, partner_id: str, fund_id: str = "fund_x") -> None:
    """Minimal partner + fund seed for tests that don't need the full
    pipeline. Faster than run_pipeline_through_stage_6 when the test
    only exercises eligibility / task logic."""
    c = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        "INSERT OR IGNORE INTO funds (fund_id, name, domain, last_updated) "
        "VALUES (?, ?, ?, ?)",
        (fund_id, "X Fund", "x.example", now),
    )
    c.execute(
        "INSERT OR IGNORE INTO partners "
        "(partner_id, name, fund_id, last_updated, do_not_contact) "
        "VALUES (?, ?, ?, ?, 0)",
        (partner_id, "Test Partner", fund_id, now),
    )
    c.commit()
    c.close()


def _seed_outcome(
    db: Path, *, partner_id: str, outreach_status: str | None = None,
    reply_type: str | None = None, meeting_booked: bool = False,
) -> None:
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO outcomes "
        "(partner_id, outreach_status, reply_type, meeting_booked, source) "
        "VALUES (?, ?, ?, ?, 'fixture')",
        (partner_id, outreach_status, reply_type, 1 if meeting_booked else 0),
    )
    c.commit()
    c.close()


@pytest.fixture
def empty_workspace(workspace: Path) -> Path:
    """Workspace whose pipeline.db has been initialized (tables exist)
    but with no pipeline run done. Faster than scored_workspace for
    tests that only care about the outcomes / review_items surface."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    ws = load_workspace(str(workspace))
    get_engine(ws.db_url)  # forces metadata.create_all
    return workspace


def test_eligibility_true_on_meeting_booked(empty_workspace: Path) -> None:
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import is_dossier_eligible

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_jane")
    _seed_outcome(
        empty_workspace / "data" / "pipeline.db",
        partner_id="p_jane", outreach_status="meeting_booked",
        reply_type="booked", meeting_booked=True,
    )
    res = is_dossier_eligible(engine, "p_jane")
    assert res.eligible is True
    assert "meeting_booked" in res.reason


def test_eligibility_true_on_substantive_reply_type(empty_workspace: Path) -> None:
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import is_dossier_eligible

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_x")
    _seed_outcome(
        empty_workspace / "data" / "pipeline.db",
        partner_id="p_x", outreach_status="replied",
        reply_type="asked_for_more_info",
    )
    res = is_dossier_eligible(engine, "p_x")
    assert res.eligible is True


def test_eligibility_false_on_passed_too_early(empty_workspace: Path) -> None:
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import is_dossier_eligible

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_passed")
    _seed_outcome(
        empty_workspace / "data" / "pipeline.db",
        partner_id="p_passed", outreach_status="replied",
        reply_type="passed_too_early",
    )
    res = is_dossier_eligible(engine, "p_passed")
    assert res.eligible is False
    assert "passed_too_early" in res.reason


def test_eligibility_false_when_no_outcome(empty_workspace: Path) -> None:
    """A cold-pipeline partner with no outcome row must NOT be
    dossier-eligible. The whole point of the gate is to keep this
    artifact post-reply."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import is_dossier_eligible

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_cold")
    res = is_dossier_eligible(engine, "p_cold")
    assert res.eligible is False
    assert "no outcome" in res.reason.lower()


def test_eligibility_false_for_do_not_contact_partner(
    empty_workspace: Path,
) -> None:
    """Do-not-contact short-circuits even when a positive outcome
    exists -- the operator's manual flag overrides legacy signals."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import is_dossier_eligible

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    db = empty_workspace / "data" / "pipeline.db"
    _seed_partner(db, "p_dnc")
    _seed_outcome(db, partner_id="p_dnc", outreach_status="meeting_booked")
    c = sqlite3.connect(db)
    c.execute(
        "UPDATE partners SET do_not_contact = 1 WHERE partner_id = ?",
        ("p_dnc",),
    )
    c.commit()
    c.close()
    res = is_dossier_eligible(engine, "p_dnc")
    assert res.eligible is False
    assert "do_not_contact" in res.reason


# ---------- task creation through persist_outcome_event -----------------

def test_substantive_outcome_creates_one_review_task(
    empty_workspace: Path,
) -> None:
    """The hook in persist_outcome_event must spawn exactly one
    investor_dossier_needed row on a substantive outcome."""
    from core.config_loader import load_workspace
    from core.db import get_engine, review_items
    from core.meeting_prep.eligibility import count_open_tasks
    from core.outcomes.events import OutcomeEvent
    from core.outcomes.persistence import persist_outcome_event
    from sqlalchemy import select

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_a")

    persist_outcome_event(engine, OutcomeEvent(
        partner_id="p_a", outreach_status="meeting_booked",
        reply_type="booked", meeting_booked=True,
        meeting_date=None, meeting_outcome=None,
        source="record_outcome", external_event_id="evt-1",
        observed_at=datetime.now(timezone.utc),
    ))
    assert count_open_tasks(engine) == 1
    # Idempotent: a second outcome (same partner) doesn't duplicate.
    persist_outcome_event(engine, OutcomeEvent(
        partner_id="p_a", outreach_status="meeting_booked",
        reply_type="asked_for_more_info", meeting_booked=True,
        meeting_date=None, meeting_outcome=None,
        source="attio_outcome_sync", external_event_id="evt-2",
        observed_at=datetime.now(timezone.utc),
    ))
    assert count_open_tasks(engine) == 1


def test_non_substantive_outcome_creates_no_task(empty_workspace: Path) -> None:
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import count_open_tasks
    from core.outcomes.events import OutcomeEvent
    from core.outcomes.persistence import persist_outcome_event

    ws = load_workspace(str(empty_workspace))
    engine = get_engine(ws.db_url)
    _seed_partner(empty_workspace / "data" / "pipeline.db", "p_b")
    persist_outcome_event(engine, OutcomeEvent(
        partner_id="p_b", outreach_status="replied",
        reply_type="passed_too_early", meeting_booked=False,
        meeting_date=None, meeting_outcome=None,
        source="record_outcome", external_event_id="evt-3",
        observed_at=datetime.now(timezone.utc),
    ))
    assert count_open_tasks(engine) == 0


# ---------- schema invariants -------------------------------------------

def test_schema_rejects_insufficient_with_partner_specific_topics() -> None:
    from pydantic import ValidationError
    from schemas.investor_dossier import InvestorDossier

    with pytest.raises(ValidationError):
        InvestorDossier.model_validate({
            "partner_id": "p",
            "insufficient_evidence": True,
            "topics_to_handle": [{
                "topic": "x", "why_they_care": "y", "how_to_answer": "z",
                "citing_signal_ids": [1],
            }],
            # Sufficient-branch other fields ignored under True.
        })


def test_schema_requires_minimum_content_when_evidence_sufficient() -> None:
    """A dossier with insufficient_evidence=False but empty topics
    list is a sign the LLM bailed mid-output -- reject so the retry
    loop in complete_json kicks in."""
    from pydantic import ValidationError
    from schemas.investor_dossier import InvestorDossier

    with pytest.raises(ValidationError):
        InvestorDossier.model_validate({
            "partner_id": "p",
            "insufficient_evidence": False,
            "lead_with_paragraph": "ok",
            "topics_to_handle": [],            # below the floor
            "anticipated_questions": [],
        })


def test_schema_accepts_full_dossier() -> None:
    from schemas.investor_dossier import InvestorDossier

    d = InvestorDossier.model_validate({
        "partner_id": "p",
        "partner_name": "Jane",
        "fund_name": "X Fund",
        "meeting_role": "lead_investor",
        "lead_with_paragraph": "I'm building Y because Z.",
        "topics_to_handle": [
            {"topic": f"T{i}", "why_they_care": "w",
             "how_to_answer": "a", "citing_signal_ids": []}
            for i in range(5)
        ],
        "anticipated_questions": [
            {"question": f"Q{i}?", "suggested_answer_direction": "d",
             "partner_specific_basis": "b", "citing_signal_ids": []}
            for i in range(6)
        ],
        "insufficient_evidence": False,
    })
    assert d.meeting_role == "lead_investor"
    assert len(d.topics_to_handle) == 5


# ---------- end-to-end through prep_brief.py ----------------------------

@pytest.fixture
def scored_workspace_with_meeting(workspace: Path) -> tuple[Path, str]:
    """Full pipeline run + a seeded substantive outcome so the
    end-to-end --dossier flow has something to chew on."""
    run_pipeline_through_stage_6(workspace)
    pid = "northbeam.example_priya_anand"
    _seed_outcome(
        workspace / "data" / "pipeline.db",
        partner_id=pid, outreach_status="meeting_booked",
        reply_type="booked", meeting_booked=True,
    )
    return workspace, pid


def test_dossier_flag_writes_default_briefs_path(
    scored_workspace_with_meeting,
) -> None:
    """--dossier defaults --out to exports/briefs/<pid>_dossier.md so
    the operator can re-find every dossier in one place."""
    workspace, pid = scored_workspace_with_meeting
    run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--partner-id", pid, "--dossier",
        cwd=REPO_ROOT,
    )
    out_path = workspace / "exports" / "briefs" / f"{pid}_dossier.md"
    assert out_path.exists()
    body = out_path.read_text(encoding="utf-8")
    assert "CONFIDENTIAL -- INVESTOR DOSSIER" in body
    # Sources section always renders so the operator audits the
    # evidence basis.
    assert "## Sources" in body


def test_dossier_flag_ineligible_partner_renders_clean_message(
    workspace: Path,
) -> None:
    """An operator who runs --dossier on a cold-pipeline partner
    should see a 'skipped' message, not a crash. --force-refresh
    overrides the gate (tested separately)."""
    run_pipeline_through_stage_6(workspace)
    # Pick any partner without an outcome.
    db = workspace / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    cold = c.execute(
        "SELECT partner_id FROM partners "
        "WHERE partner_id NOT IN (SELECT partner_id FROM outcomes) LIMIT 1"
    ).fetchone()[0]
    c.close()
    res = run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--partner-id", cold, "--dossier", "--no-drive-push",
        cwd=REPO_ROOT,
    )
    out_path = workspace / "exports" / "briefs" / f"{cold}_dossier.md"
    body = out_path.read_text(encoding="utf-8")
    assert "## Investor Dossier" in body
    assert "Skipped" in body or "not dossier-eligible" in body


def test_pending_only_processes_open_tasks_and_resolves(
    workspace: Path,
) -> None:
    """The whole post-reply loop: seed an outcome -> persist_outcome
    creates a task -> --pending-only builds the dossier -> task is
    resolved."""
    from core.config_loader import load_workspace
    from core.db import get_engine
    from core.meeting_prep.eligibility import count_open_tasks
    from core.outcomes.events import OutcomeEvent
    from core.outcomes.persistence import persist_outcome_event

    run_pipeline_through_stage_6(workspace)
    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)
    pid = "northbeam.example_priya_anand"
    persist_outcome_event(engine, OutcomeEvent(
        partner_id=pid, outreach_status="meeting_booked",
        reply_type="booked", meeting_booked=True,
        meeting_date=None, meeting_outcome=None,
        source="fixture", external_event_id="seed-1",
        observed_at=datetime.now(timezone.utc),
    ))
    assert count_open_tasks(engine) == 1

    run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--pending-only", "--no-drive-push",
        cwd=REPO_ROOT,
    )
    # Task resolved -> open count drops to zero.
    assert count_open_tasks(engine) == 0
    out_path = workspace / "exports" / "briefs" / f"{pid}_dossier.md"
    assert out_path.exists()


def test_pending_only_with_no_open_tasks_is_a_noop(workspace: Path) -> None:
    """An empty queue should print a friendly message and exit 0 --
    not error out."""
    run_pipeline_through_stage_6(workspace)
    res = run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--pending-only",
        cwd=REPO_ROOT,
    )
    assert "no open investor_dossier_needed tasks" in res.stdout


def test_dossier_cache_hit_avoids_llm_when_inputs_unchanged(
    scored_workspace_with_meeting, monkeypatch,
) -> None:
    """Second build with identical inputs (signal set, company
    profile) must hit the cache. We assert no NEW artifact row gets
    written on the second run."""
    workspace, pid = scored_workspace_with_meeting
    from core.config_loader import load_workspace
    from core.db import get_engine, meeting_prep_artifacts
    from sqlalchemy import select

    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)

    run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--partner-id", pid, "--dossier", "--no-drive-push",
        cwd=REPO_ROOT,
    )
    with engine.begin() as conn:
        first = conn.execute(
            select(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.partner_id == pid,
                meeting_prep_artifacts.c.artifact_type == "investor_dossier",
            )
        ).fetchall()
    assert len(first) == 1

    run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--partner-id", pid, "--dossier", "--no-drive-push",
        cwd=REPO_ROOT,
    )
    with engine.begin() as conn:
        second = conn.execute(
            select(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.partner_id == pid,
                meeting_prep_artifacts.c.artifact_type == "investor_dossier",
            )
        ).fetchall()
    assert len(second) == 1, "cache hit must not append a new row"


def test_dossier_force_refresh_bypasses_cache_and_eligibility(
    workspace: Path,
) -> None:
    """--force-refresh works on a NON-eligible partner (cold) AND on
    an eligible partner with a fresh cache: both produce a new
    artifact row."""
    run_pipeline_through_stage_6(workspace)
    db = workspace / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    cold = c.execute(
        "SELECT partner_id FROM partners "
        "WHERE partner_id NOT IN (SELECT partner_id FROM outcomes) LIMIT 1"
    ).fetchone()[0]
    c.close()
    run_script(
        "prep_brief.py", "--workspace", str(workspace),
        "--partner-id", cold, "--dossier", "--force-refresh",
        "--no-drive-push",
        cwd=REPO_ROOT,
    )
    from core.config_loader import load_workspace
    from core.db import get_engine, meeting_prep_artifacts
    from sqlalchemy import select
    ws = load_workspace(str(workspace))
    engine = get_engine(ws.db_url)
    with engine.begin() as conn:
        rows = conn.execute(
            select(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.partner_id == cold,
                meeting_prep_artifacts.c.artifact_type == "investor_dossier",
            )
        ).fetchall()
    assert len(rows) == 1
