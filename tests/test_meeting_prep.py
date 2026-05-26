"""Tests for Build Session 12 -- meeting-prep objection map + framing brief.

Discipline:
- LLM stays in stub mode (ANTHROPIC_API_KEY="" via conftest.run_script).
- Cache hit test counts LLM calls via LLMClient.usage so a re-run
  against an unchanged signal set MUST stay at zero.
- One markdown snapshot proves the renderer wires through end-to-end.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, run_script

# --- direct unit tests on schemas -------------------------------------

from schemas.framing_brief import FramingBriefV1
from schemas.objection_map import ObjectionMapV1


def test_objection_map_rejects_partner_specific_without_citation() -> None:
    """source != sector_norm + empty citing_signal_ids must raise --
    the brief's "no invented psychology" rule encoded at the schema."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ObjectionMapV1.model_validate({
            "partner_id": "p1",
            "insufficient_evidence": False,
            "objections": [{
                "objection": "API risk", "underlying_concern": "x",
                "source": "stated_thesis",
                "citing_signal_ids": [],  # offending field
                "strong_answer_hint": "a", "weak_answer_hint": "b",
                "severity": "high",
            }],
        })


def test_objection_map_rejects_insufficient_evidence_with_specific_objections() -> None:
    """Schema invariant: declaring evidence insufficient must coincide
    with the absence of partner-specific objections."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ObjectionMapV1.model_validate({
            "partner_id": "p1",
            "insufficient_evidence": True,
            "objections": [{
                "objection": "x", "underlying_concern": "y",
                "source": "stated_thesis", "citing_signal_ids": [1],
                "strong_answer_hint": "a", "weak_answer_hint": "b",
                "severity": "low",
            }],
        })


def test_objection_map_accepts_sector_norm_only_with_insufficient_evidence() -> None:
    """A workspace whose partner has thin signals can still surface
    generic sector-norm objections -- that's the explicit escape hatch."""
    payload = ObjectionMapV1.model_validate({
        "partner_id": "p1",
        "insufficient_evidence": True,
        "objections": [{
            "objection": "Why now?", "underlying_concern": "Timing.",
            "source": "sector_norm", "citing_signal_ids": [],
            "strong_answer_hint": "Cite the forcing function.",
            "weak_answer_hint": "Wave at TAM.",
            "severity": "medium",
        }],
    })
    assert payload.insufficient_evidence is True
    assert payload.objections[0].source == "sector_norm"


def test_framing_brief_requires_lead_and_question_when_evidence_sufficient() -> None:
    """Empty lead_with + insufficient_evidence=False means the LLM
    returned nothing useful; refuse rather than render a blank brief."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FramingBriefV1.model_validate({
            "partner_id": "p1",
            "lead_with": "",
            "amplify": ["x"],
            "question_to_ask_them": "Q?",
            "insufficient_evidence": False,
        })


# --- cache layer ------------------------------------------------------

def test_hash_signal_set_is_order_invariant() -> None:
    from core.meeting_prep.cache import hash_signal_set
    assert hash_signal_set([1, 2, 3]) == hash_signal_set([3, 1, 2])
    assert hash_signal_set([1, 2]) != hash_signal_set([1, 2, 3])


# --- renderer ---------------------------------------------------------

def test_render_objection_map_insufficient_path() -> None:
    from core.meeting_prep.render import render_objection_map
    om = ObjectionMapV1(
        partner_id="p1", objections=[],
        insufficient_evidence=True,
        notes="only 1 quality>=2 signal on file",
    )
    md = render_objection_map(om)
    assert "Insufficient evidence" in md
    assert "only 1 quality>=2 signal" in md


def test_render_framing_brief_full_shape() -> None:
    from core.meeting_prep.render import render_framing_brief
    fb = FramingBriefV1(
        partner_id="p1",
        lead_with="Lead with negative churn.",
        amplify=["Show retention cohorts.", "Frame the wedge."],
        address_unprompted=["API concentration risk."],
        do_not_lead_with=["TAM-first framing."],
        question_to_ask_them="What did you learn from Acme?",
        citing_signal_ids=[12, 47],
        insufficient_evidence=False,
    )
    md = render_framing_brief(fb)
    assert "Lead with negative churn." in md
    assert "Show retention cohorts." in md
    assert "API concentration risk." in md
    assert "TAM-first framing." in md
    assert "What did you learn from Acme?" in md
    assert "12, 47" in md


# --- integration: build via prep_brief.py through stub LLM ------------

@pytest.fixture
def scored_workspace(_scored_workspace_source: Path, tmp_path: Path) -> Path:
    """Per-test copy of the session-cached post-stage-6 workspace,
    with an outcome row pre-seeded for the meeting-prep auto-include
    branch.

    Was running stages 1-6 per-test until CI-perf landed; the
    pipeline now runs ONCE per session (in conftest's
    `_scored_workspace_source`), each test copytree's from that
    cache, and only the per-test outcome seed runs here.
    """
    import shutil  # noqa: PLC0415 - module-level shutil already imported
    dst = tmp_path / "test_workspace"
    shutil.copytree(_scored_workspace_source, dst)
    _seed_outcome(
        dst / "data" / "pipeline.db",
        partner_id="northbeam.example_priya_anand",
        outreach_status="meeting_booked",
    )
    return dst


def _seed_outcome(db: Path, *, partner_id: str, outreach_status: str) -> None:
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO outcomes (partner_id, outreach_status, source) "
        "VALUES (?, ?, 'fixture')",
        (partner_id, outreach_status),
    )
    c.commit()
    c.close()


def test_prep_brief_auto_includes_on_meeting_booked(
    scored_workspace: Path, capsys,
) -> None:
    """The headline behavior: when a partner has outreach_status =
    meeting_booked, prep_brief.py auto-runs both LLM builders without
    explicit flags. Stub mode keeps the test offline."""
    res = run_script(
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", "northbeam.example_priya_anand",
        cwd=REPO_ROOT,
    )
    assert "## Objections to prepare for" in res.stdout
    assert "## How to tell your story today" in res.stdout
    # Stub responses both declare insufficient_evidence=True (the
    # stub is intentionally minimal); the renderer must surface that
    # rather than fabricating.
    assert "Insufficient evidence" in res.stdout


def test_prep_brief_skips_llm_sections_for_cold_partner(
    scored_workspace: Path,
) -> None:
    """A partner with no outcome row (cold pipeline) must NOT auto-trigger
    LLM calls -- the budget gate the spec promised."""
    # Pick any partner WITHOUT a seeded outcome.
    db = scored_workspace / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    row = c.execute(
        "SELECT partner_id FROM partners "
        "WHERE partner_id NOT IN (SELECT partner_id FROM outcomes) "
        "LIMIT 1",
    ).fetchone()
    c.close()
    assert row is not None, "fixture should have a partner with no outcome"
    cold_partner = row[0]

    res = run_script(
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", cold_partner,
        cwd=REPO_ROOT,
    )
    assert "## Objections to prepare for" not in res.stdout
    assert "## How to tell your story today" not in res.stdout


def test_prep_brief_explicit_opt_in_for_cold_partner(
    scored_workspace: Path,
) -> None:
    """--include-objections / --include-framing override the
    auto-include gate so cold-pipeline operators can spend LLM time
    deliberately."""
    db = scored_workspace / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    row = c.execute(
        "SELECT partner_id FROM partners "
        "WHERE partner_id NOT IN (SELECT partner_id FROM outcomes) "
        "LIMIT 1",
    ).fetchone()
    c.close()
    cold_partner = row[0]

    res = run_script(
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", cold_partner,
        "--include-objections", "--include-framing",
        cwd=REPO_ROOT,
    )
    assert "## Objections to prepare for" in res.stdout
    assert "## How to tell your story today" in res.stdout


def test_cache_persists_and_short_circuits_repeat_runs(
    scored_workspace: Path,
) -> None:
    """Re-running prep_brief.py against an unchanged signal set must
    produce a meeting_prep_artifacts row on the first call and
    return zero new rows (cache hit) on the second."""
    db = scored_workspace / "data" / "pipeline.db"
    pid = "northbeam.example_priya_anand"
    args = (
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", pid,
    )
    run_script(*args, cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    first_rows = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts "
        "WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    c.close()
    # Expect both artifact_type rows: objection_map + framing_brief.
    assert first_rows == 2

    run_script(*args, cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    second_rows = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts "
        "WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    c.close()
    # Unchanged signal set -> no new rows written.
    assert second_rows == first_rows == 2


def test_cache_busts_when_force_rebuild_flag_passed(
    scored_workspace: Path,
) -> None:
    """--force-rebuild bypasses the cache; new artifact rows appear
    even though the signal set is unchanged."""
    db = scored_workspace / "data" / "pipeline.db"
    pid = "northbeam.example_priya_anand"
    base = (
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", pid,
    )
    run_script(*base, cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    first = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    c.close()

    run_script(*base, "--force-rebuild", cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    second = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    c.close()
    # Each artifact_type appended once on the rebuild = +2 rows.
    assert second == first + 2


def test_cache_busts_when_signal_set_changes(
    scored_workspace: Path,
) -> None:
    """The cache is keyed on the signal_set hash, so flipping one
    signal's verification flag invalidates the row without needing
    --force-rebuild."""
    db = scored_workspace / "data" / "pipeline.db"
    pid = "northbeam.example_priya_anand"
    base = (
        "prep_brief.py", "--workspace", str(scored_workspace),
        "--partner-id", pid,
    )
    run_script(*base, cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    first = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    # Knock one verified, quality>=2 signal out of the partner's set.
    # SQLite's UPDATE LIMIT is a compile-time flag; route through a
    # subquery on signal_id for portability.
    target = c.execute(
        "SELECT signal_id FROM signals "
        "WHERE partner_id = ? AND verified = 1 "
        "AND signal_quality_score >= 2 LIMIT 1",
        (pid,),
    ).fetchone()
    assert target is not None, "fixture should have a quality>=2 signal"
    c.execute(
        "UPDATE signals SET verified = 0 WHERE signal_id = ?",
        (target[0],),
    )
    c.commit()
    c.close()

    run_script(*base, cwd=REPO_ROOT)
    c = sqlite3.connect(db)
    second = c.execute(
        "SELECT count(*) FROM meeting_prep_artifacts WHERE partner_id = ?",
        (pid,),
    ).fetchone()[0]
    c.close()
    # New hash -> new rows for both artifact types.
    assert second == first + 2
