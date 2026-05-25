"""Stage-specific tests split out from tests/test_smoke.py.

Refactor item 23: per-stage test files so changes to one stage do not
churn a 4200-line monolith.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# REPO_ROOT, _run, _counts come from tests/conftest.py (Refactor item 24).
from tests.conftest import REPO_ROOT, _run, _counts





def test_stage5_clears_quality_on_unverified():
    """Batch 11 (#351/#352): if Stage 5 re-runs and a previously-verified
    signal flips to unverified, its signal_quality_score and
    quality_reasoning must be cleared so Stage 6's quality>=2 filter and
    Stage 7's signal_led eligibility don't pick up stale quality data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # Pick a verified signal with a real quality score, then break its
        # quoted_text so re-verification fails, then re-run Stage 5 --force.
        c = sqlite3.connect(db)
        row = c.execute(
            "select signal_id, quoted_text, signal_quality_score, "
            "quality_reasoning from signals where verified=1 and "
            "signal_quality_score >= 2 limit 1"
        ).fetchone()
        assert row is not None, "fixture should produce at least one verified q2+ signal"
        sid, old_quote, old_quality, old_reasoning = row
        assert old_quality is not None and old_reasoning, (
            "baseline: row should have non-null quality + reasoning"
        )
        # Mutate the quoted text to something that can't be verified anywhere.
        c.execute(
            "update signals set quoted_text = ? where signal_id = ?",
            (
                "this quote could not possibly appear in any real source page "
                "because it was constructed solely for the regression test",
                sid,
            ),
        )
        c.commit()
        c.close()

        _run("05_verify_and_quality.py", "--workspace", ws, "--force",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        verified, qscore, qreason = c.execute(
            "select verified, signal_quality_score, quality_reasoning "
            "from signals where signal_id = ?",
            (sid,),
        ).fetchone()
        c.close()
        assert verified == 0, "signal should now be unverified"
        assert qscore is None, (
            f"signal_quality_score should be cleared on unverified "
            f"transition; still {qscore}"
        )
        assert qreason is None, (
            f"quality_reasoning should be cleared on unverified transition; "
            f"still {qreason!r}"
        )





def test_batch28_stage5_offline_mode():
    """Inventory #354: Stage 5 --offline skips live fetch and verifies
    only against captured snapshots."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # --offline run.
        _run("05_verify_and_quality.py", "--workspace", ws, "--offline",
             cwd=REPO_ROOT)
        c = sqlite3.connect(db)
        # Every verification should use snapshot_fallback (or one of the
        # offline-failure methods); none should be live_match.
        methods = c.execute(
            "select distinct verification_method from signals"
        ).fetchall()
        c.close()
        method_set = {m[0] for m in methods}
        assert "live_match" not in method_set, (
            f"offline mode must not produce live_match; got {method_set}"
        )
        assert "snapshot_fallback" in method_set, (
            f"offline mode should produce snapshot_fallback; got {method_set}"
        )
