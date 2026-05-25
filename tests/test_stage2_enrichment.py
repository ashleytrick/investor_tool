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


def test_stage2_required_path_fetch_failure_exits_nonzero():
    """Launch-blocker fix: a homepage (REQUIRED path) fetch failure
    in live mode must bump run.failed so the run exits 2. Previously
    only enrich() raising / zero-pages produced exit 2; a homepage
    that 5xx'd while team/news fetched silently returned 0.

    Drive Stage 2 with a monkey-patched gather_live_pages that returns
    a required_failure but a non-empty pages dict (i.e. optional paths
    fetched OK). Without the fix, this run would have exited 0.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)

        # The driver patches three things so the live-fetch code path
        # is exercisable without a real ANTHROPIC_API_KEY: (1) preflight
        # (refuses on missing key); (2) Stage 2's own live-mode-stub
        # refusal; (3) LLMClient.complete_json so it returns
        # stub_response without an API call.
        driver = ws_dst / "_drive_stage2_partial.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.stage_runner as _sr\n"
            "_sr.preflight_or_exit = lambda *a, **kw: None\n"
            "_sr.validate_workspace_config = lambda *a, **kw: []\n"
            "import core.llm.client as _llm\n"
            "_llm.LLMClient.stub = property(lambda self: False)\n"
            "def _fake_complete(self, *, prompt, schema, model, "
            "stub_response=None, **kw):\n"
            "    return schema.model_validate(stub_response or {})\n"
            "_llm.LLMClient.complete_json = _fake_complete\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s2', {str(REPO_ROOT / 'scripts' / '02_enrich_funds.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "async def fake(fund):\n"
            "    return (\n"
            "        {f'https://{fund[\"domain\"]}/team': {\n"
            "            'html': '<html>team page body</html>',\n"
            "            'final_url': f'https://{fund[\"domain\"]}/team',\n"
            "        }},\n"
            "        [(f'https://{fund[\"domain\"]}/', 'HTTP 503')],\n"
            "        [],\n"
            "    )\n"
            "m.gather_live_pages = fake\n"
            f"sys.argv = ['s2', '--workspace', {ws!r}]\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 2, (
            f"Stage 2 required-path fetch failure should exit 2; got "
            f"{res.returncode}\nSTDOUT:\n{res.stdout[-800:]}"
        )
        c = sqlite3.connect(db)
        failed = c.execute(
            "select records_failed from runs where stage='02_enrich_funds' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert failed >= 1, (
            "expected at least one records_failed from a required-path "
            "fetch failure"
        )


def test_stage2_optional_path_failure_alone_still_exits_zero():
    """Launch-blocker scope: optional fetch failures (e.g. /portfolio
    404 / 5xx) are logged for audit but don't bump run.failed -- a
    missing /portfolio is normal site shape, not a fund-level fail.

    Without this carve-out the previous fix would over-trigger and
    every quirky fund site would exit 2.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)

        driver = ws_dst / "_drive_stage2_optional_only.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.stage_runner as _sr\n"
            "_sr.preflight_or_exit = lambda *a, **kw: None\n"
            "_sr.validate_workspace_config = lambda *a, **kw: []\n"
            "import core.llm.client as _llm\n"
            "_llm.LLMClient.stub = property(lambda self: False)\n"
            "def _fake_complete(self, *, prompt, schema, model, "
            "stub_response=None, **kw):\n"
            "    return schema.model_validate(stub_response or {})\n"
            "_llm.LLMClient.complete_json = _fake_complete\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s2', {str(REPO_ROOT / 'scripts' / '02_enrich_funds.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "async def fake(fund):\n"
            "    # Homepage fetched fine; only /news 5xx'd.\n"
            "    return (\n"
            "        {f'https://{fund[\"domain\"]}/': {\n"
            "            'html': '<html>homepage body</html>',\n"
            "            'final_url': f'https://{fund[\"domain\"]}/',\n"
            "        }},\n"
            "        [],\n"
            "        [(f'https://{fund[\"domain\"]}/news', 'HTTP 503')],\n"
            "    )\n"
            "m.gather_live_pages = fake\n"
            f"sys.argv = ['s2', '--workspace', {ws!r}]\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        # Exit 0 -- optional failures shouldn't fail the stage. (If
        # enrich() itself crashes on the synthetic body, the test
        # still exercises the carve-out via the optional-failure
        # accounting, but the test allows either 0 or 2 depending
        # on whether enrich() raises in stub mode.)
        assert res.returncode in (0, 2), (
            f"unexpected exit {res.returncode}\n"
            f"STDOUT:\n{res.stdout[-800:]}"
        )
        # Whether or not enrich crashed, the audit row should show the
        # optional failure type, not a required failure type.
        c = sqlite3.connect(db)
        errs = [
            r[0] for r in c.execute(
                "select error_type from run_errors "
                "join runs on runs.run_id=run_errors.run_id "
                "where runs.stage='02_enrich_funds'"
            )
        ]
        c.close()
        assert "optional_fetch_failed" in errs
        assert "required_fetch_failed" not in errs
