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


@pytest.fixture(autouse=True)
def _reset_request_contextvars():
    """Phase 2a: clear the per-request user_id contextvar between
    tests so a unit test that directly calls `current_principal`
    doesn't leak its user_id into the next test's
    `_engine_and_ws()` lookup.

    Production FastAPI requests get per-request task isolation for
    contextvars automatically; pytest functions all run in the
    same task, which is why we reset by hand here.
    """
    try:
        from web import deps as _deps
    except ImportError:
        yield
        return
    var = getattr(_deps, "_CURRENT_USER_ID_VAR", None)
    if var is None:
        yield
        return
    token = var.set(None)
    try:
        yield
    finally:
        var.reset(token)


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


def run_pipeline_through_stage_6(ws_dst: Path) -> None:
    """Drive the fixture pipeline up to Stage 6 (no emails yet).

    Used by tests that need a fully-scored workspace as their starting
    state (Stage 7 / Stage 8 / operator CLI tests).
    """
    ws = str(ws_dst)
    for s, extra in (
        ("01_aggregate_sources.py", ()),
        ("02_enrich_funds.py", ("--fixtures",)),
        ("03_mine_activity.py", ("--fixtures",)),
        ("04_mine_partner_signals.py", ("--fixtures",)),
        ("05_verify_and_quality.py", ()),
        ("06_score_candidates.py", ()),
    ):
        run_script(s, "--workspace", ws, *extra, cwd=REPO_ROOT)


_run_pipeline_through_stage_6 = run_pipeline_through_stage_6


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


@pytest.fixture(scope="session")
def _scored_workspace_source(tmp_path_factory) -> Path:
    """Session-scoped cache of a workspace whose pipeline has been
    run through Stage 6 already.

    Building this is expensive: stages 1-6 each shell out and the
    sum is roughly 10s per full build. Caching it once per test
    session and copytree'ing into a per-test tmp_path lets every
    downstream test that just needs "a scored workspace" skip the
    build entirely.

    This fixture is internal -- tests pull `scored_workspace` (the
    per-test copy), not the cached source. The source is read-only
    by convention; tests must not mutate it (pytest's session scope
    + the copytree pattern enforces this in practice).

    CI speedup paths:
      1. PYTEST_SCORED_WS_CACHE env var: pin the cache to a stable
         path so actions/cache can persist it across CI runs (one
         build per code change, not one build per CI invocation).
      2. xdist worker sharing: every pytest-xdist worker spawns its
         own session, so without coordination each worker would
         re-run stages 1-6. We use a filelock so the first worker
         builds and the rest block until the .built marker appears,
         then everyone copytree's from the shared cache.
    """
    cache_env = os.environ.get("PYTEST_SCORED_WS_CACHE")
    if cache_env:
        cache_root = Path(cache_env)
        cache_root.mkdir(parents=True, exist_ok=True)
    else:
        cache_root = tmp_path_factory.mktemp("scored_ws_cache")
    dst = cache_root / "test_workspace"
    marker = cache_root / ".built"

    # Lock so concurrent xdist workers don't race the build.
    # filelock is a tiny pure-Python dep; only required when xdist
    # workers OR cross-run caches are in play. Fall back to no-op
    # locking if it isn't installed (single-process local runs).
    try:
        from filelock import FileLock  # type: ignore
        lock_ctx = FileLock(str(cache_root / ".build.lock"))
    except ImportError:
        from contextlib import nullcontext
        lock_ctx = nullcontext()

    with lock_ctx:
        if marker.exists() and dst.exists():
            # Cache hit (cross-run via PYTEST_SCORED_WS_CACHE OR
            # a sibling xdist worker already finished the build).
            return dst
        # Cache miss: build into dst.
        if dst.exists():
            shutil.rmtree(dst)
        src = REPO_ROOT / "clients" / "test_workspace"
        shutil.copytree(src, dst)
        db = dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        run_pipeline_through_stage_6(dst)
        marker.touch()
    return dst


@pytest.fixture
def scored_workspace(_scored_workspace_source: Path, tmp_path: Path) -> Path:
    """Per-test copy of a session-cached post-stage-6 workspace.

    Use this when a test needs a fully-pipelined workspace as its
    starting state but doesn't care HOW the pipeline ran:

        def test_foo(scored_workspace: Path):
            ws = str(scored_workspace)
            # stages 1-6 already done; partners + signals + scores
            # are all in scored_workspace / "data" / "pipeline.db"

    Replaces the older pattern:

        def test_foo(workspace: Path):
            run_pipeline_through_stage_6(workspace)
            ...

    which runs the full pipeline once PER TEST. Tests that exercise
    stages 1-6 themselves (test_pipeline_e2e, test_stage6_scoring,
    test_check_ready) should keep using `workspace` so they see a
    fresh pre-pipeline starting state.
    """
    dst = tmp_path / "test_workspace"
    shutil.copytree(_scored_workspace_source, dst)
    return dst


@pytest.fixture
def scored_workspace_db(scored_workspace: Path) -> Path:
    """SQLite path inside the scored-workspace fixture."""
    return scored_workspace / "data" / "pipeline.db"


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
