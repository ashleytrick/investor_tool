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





def test_batch43_stage2_partial_failure_exits_nonzero():
    """Inventory #83: Stage 2 exits 2 when any per-fund enrichment
    raises. Verifies the Batch 35 fix is wired through enrich()."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)

        # Drive Stage 2 with a monkey-patched enrich() that raises on
        # the first fund (same pattern as Batch 11's Stage 6 test).
        driver = ws_dst / "_drive_stage2_fail.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s2', {str(REPO_ROOT / 'scripts' / '02_enrich_funds.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real = m.enrich\n"
            "calls = {'n': 0}\n"
            "def boom(*a, **kw):\n"
            "    calls['n'] += 1\n"
            "    if calls['n'] == 1:\n"
            "        raise RuntimeError('synthetic Stage 2 failure for #83')\n"
            "    return real(*a, **kw)\n"
            "m.enrich = boom\n"
            f"sys.argv = ['s2', '--workspace', {ws!r}, '--fixtures']\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 2, (
            f"Stage 2 partial failure should exit 2; got {res.returncode}\n"
            f"STDOUT:\n{res.stdout[-800:]}"
        )

        c = sqlite3.connect(db)
        failed = c.execute(
            "select records_failed from runs where stage='02_enrich_funds' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert failed >= 1
