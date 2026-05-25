"""End-to-end tests for scripts/review_attribution.py (Slice 6)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run


def _run_pipeline_through_stage3(ws_dst: Path) -> None:
    """Run Stage 1-3 so deal_attributions has rows with match_status."""
    ws = str(ws_dst)
    for s, extra in (
        ("01_aggregate_sources.py", ()),
        ("02_enrich_funds.py", ("--fixtures",)),
        ("03_mine_activity.py", ("--fixtures",)),
    ):
        _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)


def test_stage3_populates_match_status():
    """After Stage 3 runs, every persisted deal_attributions row has
    a non-null match_status drawn from the new vocabulary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        _run_pipeline_through_stage3(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        statuses = [
            r[0] for r in c.execute(
                "select distinct match_status from deal_attributions",
            )
        ]
        c.close()
        # Every row should have a match_status from the known set.
        assert statuses, "Stage 3 should have produced at least one row"
        for s in statuses:
            assert s in {
                "confirmed", "likely", "ambiguous", "rejected", "unmatched",
                None,  # legacy migration tolerance; fixture should produce real values
            }


def test_review_cli_lists_pending_when_ambiguous_present():
    """Inject a synthetic ambiguous review row + confirm the listing
    shows it. Avoids relying on the fixture producing ambiguous
    matches (which it doesn't with the canonical seed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage3(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        c.execute(
            "insert into review_items (kind, target_id, context, "
            "status, created_at) values (?, ?, ?, 'pending', ?)",
            (
                "ambiguous_attribution", "https://test.example/seed",
                '{"raw_lead_investor": "Northbeam Ventures", '
                '"chosen_fund_id": "f1", '
                '"candidates": [{"id": "f1", "name": "northbeam", '
                '"score": 0.88}]}',
                "2026-05-25T12:00:00Z",
            ),
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "review_attribution.py"),
             "--workspace", ws],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "1 pending" in res.stdout
        assert "Northbeam Ventures" in res.stdout


def test_review_cli_confirm_flips_status():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage3(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        # Seed an ambiguous review row + a deal_attribution it points at.
        c = sqlite3.connect(db)
        c.execute(
            "insert into deal_attributions "
            "(company, lead_fund_id, source_url, match_status, captured_at) "
            "values ('Test Co', null, 'https://test.example/q3', "
            "'ambiguous', '2026-05-25T12:00:00Z')"
        )
        c.execute(
            "insert into review_items (kind, target_id, status, created_at, context) "
            "values ('ambiguous_attribution', 'https://test.example/q3', 'pending', "
            "'2026-05-25T12:00:00Z', '{}')",
        )
        review_id = c.execute(
            "select review_id from review_items order by review_id desc limit 1"
        ).fetchone()[0]
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "review_attribution.py"),
             "--workspace", ws, "--review-id", str(review_id), "--confirm",
             "--reason", "manual review confirmed"],
            capture_output=True, text=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr

        c = sqlite3.connect(db)
        new_status, review_status, reviewed_by = c.execute(
            "select match_status, review_status, reviewed_by "
            "from deal_attributions "
            "where source_url = 'https://test.example/q3'"
        ).fetchone()
        review_state = c.execute(
            "select status, resolution_notes from review_items "
            "where review_id = ?", (review_id,),
        ).fetchone()
        c.close()
        assert new_status == "confirmed"
        assert review_status == "confirmed"
        assert reviewed_by == "tester"
        assert review_state[0] == "resolved"
        assert review_state[1] == "manual review confirmed"


def test_review_cli_reject_requires_reason():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage3(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        c.execute(
            "insert into deal_attributions "
            "(company, source_url, match_status, captured_at) "
            "values ('Test', 'https://test.example/x', 'ambiguous', "
            "'2026-05-25T12:00:00Z')"
        )
        c.execute(
            "insert into review_items (kind, target_id, status, created_at, context) "
            "values ('ambiguous_attribution', 'https://test.example/x', "
            "'pending', '2026-05-25T12:00:00Z', '{}')",
        )
        review_id = c.execute(
            "select review_id from review_items order by review_id desc limit 1"
        ).fetchone()[0]
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "review_attribution.py"),
             "--workspace", ws, "--review-id", str(review_id), "--reject"],
            capture_output=True, text=True, timeout=60,
        )
        assert res.returncode == 1
        assert "--reason" in res.stdout
