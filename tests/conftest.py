"""Shared test fixtures + helpers.

Split out from the original tests/test_smoke.py (Refactor item 24). New
per-stage test files import REPO_ROOT/_run/_counts from this module
and use the `workspace` fixture instead of repeating
TemporaryDirectory + shutil.copytree boilerplate in every test.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Allow `from core.db import ...` in tests that exercise the library
# directly. pytest is launched from the repo root and doesn't otherwise
# add the project root to sys.path.
sys.path.insert(0, str(REPO_ROOT))


def run_script(script: str, *args: str, cwd: Path | None = None,
               check: bool = True, timeout: int = 120,
               env_overrides: dict[str, str] | None = None,
               ) -> subprocess.CompletedProcess:
    """Run a scripts/<script> file. Defaults to stub-mode LLM, no Attio.

    Tests that need a specific failure exit code pass check=False and
    inspect res.returncode themselves.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / script), *args]
    env = {**os.environ, "ANTHROPIC_API_KEY": ""}
    if env_overrides:
        env.update(env_overrides)
    res = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=cwd or REPO_ROOT, env=env, timeout=timeout,
    )
    if check:
        assert res.returncode == 0, (
            f"{script} exited {res.returncode}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return res


# Back-compat alias used by the original test_smoke.py module.
_run = run_script


def table_counts(db: Path) -> dict[str, int]:
    """Return row counts for the standard pipeline tables. Used by
    end-to-end tests to assert headline invariants."""
    c = sqlite3.connect(db)
    tables = [
        "funds", "partners", "signals", "deal_attributions",
        "partner_score_summaries", "scores", "email_drafts",
        "followup_drafts", "deck_request_responses", "source_snapshots",
        "batch_qa_reports", "runs",
    ]
    out = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    c.close()
    return out


# Back-compat alias.
_counts = table_counts


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Fresh copy of clients/test_workspace in a per-test temp dir.

    Replaces the boilerplate:

        with tempfile.TemporaryDirectory() as tmpdir:
            ws_src = REPO_ROOT / "clients" / "test_workspace"
            ws_dst = Path(tmpdir) / "test_workspace"
            shutil.copytree(ws_src, ws_dst)
            db = ws_dst / "data" / "pipeline.db"
            if db.exists():
                db.unlink()
            ws = str(ws_dst)
            ...

    With:

        def test_foo(workspace):
            ws = str(workspace)
            # workspace is a Path; pipeline.db has been pre-wiped.

    Tests that need the DB Path directly use `workspace_db` below.
    """
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "test_workspace"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    return dst


@pytest.fixture
def workspace_db(workspace: Path) -> Path:
    """The pipeline.db path inside the fresh workspace fixture."""
    return workspace / "data" / "pipeline.db"


@pytest.fixture
def workspace_factory(tmp_path_factory):
    """Multi-workspace variant. Tests that need >1 isolated workspace
    (e.g. cross-workspace data leakage tests) call it directly:

        def test_isolation(workspace_factory):
            ws_a = workspace_factory()
            ws_b = workspace_factory()
            # ws_a and ws_b are independent fresh copies.
    """
    src = REPO_ROOT / "clients" / "test_workspace"
    counter = {"n": 0}

    def make() -> Path:
        counter["n"] += 1
        root = tmp_path_factory.mktemp(f"ws{counter['n']}")
        dst = root / "test_workspace"
        shutil.copytree(src, dst)
        db = dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        return dst

    return make
