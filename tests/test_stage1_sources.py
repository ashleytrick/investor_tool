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





def test_batch36_stage1_required_source_blocks():
    """Inventory #7: a source with required: true that fails to load
    must cause Stage 1 to exit 2."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        # Replace sources.yaml so the seed CSV is REQUIRED but the path
        # points to a non-existent file.
        (ws_dst / "config" / "sources.yaml").write_text(
            "public_lists:\n"
            "  - name: 'Missing Required'\n"
            "    path: 'data/raw/does-not-exist.csv'\n"
            "    parser: csv\n"
            "    required: true\n"
            "funding_announcement_feeds: []\n"
            "partner_signal_sources:\n"
            "  podcast_search_api: 'listennotes'\n",
            encoding="utf-8",
        )
        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "01_aggregate_sources.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2
        assert "REQUIRED" in res.stdout
