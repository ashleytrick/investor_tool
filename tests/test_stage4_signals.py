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





def test_batch36_stage4_csv_validation_and_unknown_partner():
    """Inventory #10/#11: Stage 4 validates partner_content_urls.csv
    header upfront and refuses unknown partner_ids in live mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)
        # Set fake ANTHROPIC_API_KEY so Stage 4 preflight (which requires
        # the key in live mode) doesn't refuse before our CSV checks run.
        env = {**os.environ, "ANTHROPIC_API_KEY": "fake-key-for-csv-tests"}

        # #10: write a malformed CSV missing the partner_id column.
        # data/raw/ isn't tracked in git (empty dir), so on a fresh CI
        # checkout the parent doesn't exist. Create it before writing.
        bad = ws_dst / "data" / "raw" / "partner_content_urls.csv"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("source_type,source_url\nblog,https://x.example/p\n",
                       encoding="utf-8")
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "04_mine_partner_signals.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2, (
            f"expected refusal on malformed CSV; got {res.returncode}\n"
            f"STDOUT:\n{res.stdout}"
        )
        assert "missing required column" in res.stdout

        # #11: well-formed CSV with an unknown partner_id.
        bad.write_text(
            "partner_id,source_type,source_url\n"
            "not-a-real-partner-id,blog,https://x.example/p\n",
            encoding="utf-8",
        )
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "04_mine_partner_signals.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        # Stage 4 exits non-zero either via our refusal OR the LLM live-key
        # check; the important thing is the run_errors row was logged.
        c = sqlite3.connect(db)
        n_unknown_errors = c.execute(
            "select count(*) from run_errors "
            "where error_type='unknown_partner_in_csv'"
        ).fetchone()[0]
        c.close()
        assert n_unknown_errors >= 1, (
            f"expected unknown_partner_in_csv run_error; got 0"
        )
