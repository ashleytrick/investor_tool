"""Review item #8: wizard runs Stages 1-5 before /pipeline/score.

Pre-fix: only Stages 6 + 7 had endpoints. A fresh workspace could
call /pipeline/score with no funds / partners / signals and get
nothing useful back. The wizard had no way to populate the
pipeline through the API.

Post-fix: new POST /pipeline/{aggregate,enrich,activity,
partner-signals,verify} (one per stage) PLUS a meta endpoint
POST /pipeline/ingest that runs all five in sequence.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = REPO_ROOT / "clients" / "test_workspace"
    dst = tmp_path / "ws"
    shutil.copytree(src, dst)
    db = dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    from core.db import get_engine
    get_engine(f"sqlite:///{db}")
    return dst


@pytest.fixture
def client(workspace: Path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-api-key")
    monkeypatch.setenv("INVESTOR_WORKSPACE", str(workspace))
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("API_ALLOW_EXAMPLE_DOMAINS", "true")
    import importlib
    import web.api as api_mod
    importlib.reload(api_mod)
    from fastapi.testclient import TestClient
    return TestClient(api_mod.app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-api-key"}


class _FakeRes:
    def __init__(self, returncode=0, stdout="ok\n", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------- per-stage endpoints ----------

@pytest.mark.parametrize(
    "path,expected_script",
    [
        ("/pipeline/aggregate", "01_aggregate_sources.py"),
        ("/pipeline/enrich", "02_enrich_funds.py"),
        ("/pipeline/activity", "03_mine_activity.py"),
        ("/pipeline/partner-signals", "04_mine_partner_signals.py"),
        ("/pipeline/verify", "05_verify_and_quality.py"),
    ],
)
def test_per_stage_endpoint_shells_out_to_right_script(
    client, monkeypatch, path: str, expected_script: str,
) -> None:
    captured: list[tuple] = []

    def fake_run(*args, timeout=120):
        captured.append(args)
        return _FakeRes()

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)

    res = client.post(path, headers=_auth())
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert captured[0][0] == expected_script


def test_per_stage_endpoint_propagates_nonzero_returncode(
    client, monkeypatch,
) -> None:
    def fake_run(*args, timeout=120):
        return _FakeRes(returncode=2, stdout="", stderr="boom")

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)
    res = client.post("/pipeline/aggregate", headers=_auth())
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "aggregate_sources" in detail["error"]


def test_per_stage_endpoint_requires_auth(client) -> None:
    for path in (
        "/pipeline/aggregate", "/pipeline/enrich",
        "/pipeline/activity", "/pipeline/partner-signals",
        "/pipeline/verify",
    ):
        assert client.post(path).status_code == 401, path


# ---------- /pipeline/ingest meta endpoint ----------

def test_ingest_runs_all_five_stages_in_order(client, monkeypatch) -> None:
    captured: list[str] = []

    def fake_run(*args, timeout=120):
        captured.append(args[0])
        return _FakeRes()

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)

    res = client.post("/pipeline/ingest", headers=_auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert len(body["stages"]) == 5
    # Stages run in script-number order.
    assert captured == [
        "01_aggregate_sources.py",
        "02_enrich_funds.py",
        "03_mine_activity.py",
        "04_mine_partner_signals.py",
        "05_verify_and_quality.py",
    ]
    # Each stage carries its own ok/returncode.
    for s in body["stages"]:
        assert s["ok"] is True
        assert s["returncode"] == 0


def test_ingest_aborts_remaining_stages_on_failure(
    client, monkeypatch,
) -> None:
    """Stage 3 fails -> Stage 4 + 5 must NOT run. Response is
    ok=False with the failing stage clearly marked."""
    captured: list[str] = []

    def fake_run(*args, timeout=120):
        captured.append(args[0])
        if args[0] == "03_mine_activity.py":
            return _FakeRes(
                returncode=2, stdout="", stderr="enrichment outage",
            )
        return _FakeRes()

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)

    res = client.post("/pipeline/ingest", headers=_auth())
    assert res.status_code == 200  # endpoint is 200, ok=False in body
    body = res.json()
    assert body["ok"] is False
    # Only the first three scripts should have run.
    assert captured == [
        "01_aggregate_sources.py",
        "02_enrich_funds.py",
        "03_mine_activity.py",
    ]
    stages = body["stages"]
    assert stages[0]["ok"] is True
    assert stages[1]["ok"] is True
    assert stages[2]["ok"] is False
    assert stages[2]["stage"] == "mine_activity"
    assert "outage" in stages[2]["stderr"]


def test_ingest_handles_runner_exception_as_failed_stage(
    client, monkeypatch,
) -> None:
    """A Python exception thrown by the runner (e.g. file not
    found, OS error) lands as a structured failure entry, not a
    500."""
    def fake_run(*args, timeout=120):
        raise RuntimeError("subprocess timed out")

    import web.api as api_mod
    monkeypatch.setattr(api_mod, "_run_cli", fake_run)

    res = client.post("/pipeline/ingest", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["stages"][0]["ok"] is False
    assert body["stages"][0]["stage"] == "aggregate_sources"


def test_ingest_requires_auth(client) -> None:
    assert client.post("/pipeline/ingest").status_code == 401
