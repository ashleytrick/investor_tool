"""Review items #9 + #10: /pipeline/sources rejects CSVs whose
headers Stage 1 can't recognize, and reports the count of USABLE
rows (those with a firm-name) instead of total spreadsheet rows.
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
    dst = tmp_path / "test_workspace"
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


# ---------- #10: header validation ----------

def test_upload_rejects_csv_without_firm_name_column(client) -> None:
    """A CSV whose headers don't match any of name / firm /
    investor / fund must 400 -- Stage 1 would silently ingest
    zero rows otherwise."""
    payload = (
        b"foo,bar\n"
        b"alpha,beta\n"
        b"gamma,delta\n"
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("misshapen.csv", payload, "text/csv")},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "firm" in detail["error"].lower() or "name" in detail["error"].lower()
    # The error should hint at the recognized header set so the
    # operator can rename a column without guessing.
    assert "name" in detail["error"] or "firm" in detail["error"]


def test_upload_accepts_csv_with_firm_alias_column(client) -> None:
    """`firm` is an accepted alias (alongside name / investor /
    fund). A CSV using `firm` should succeed."""
    payload = (
        b"firm,domain\n"
        b"Northbeam,northbeam.example\n"
        b"Apex Ventures,apex.example\n"
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("firm.csv", payload, "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["row_count"] == 2


def test_upload_accepts_csv_with_only_firm_no_domain(client) -> None:
    """Stage 1 can enrich the domain later; firm name is the only
    hard requirement."""
    payload = (
        b"name\n"
        b"Northbeam\n"
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("name_only.csv", payload, "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["row_count"] == 1


def test_upload_recognizes_investor_name_alias(client) -> None:
    """OpenVC's CSV exports use `Investor name` (case-insensitive)."""
    payload = (
        b"Investor name,Website\n"
        b"Acme Capital,acme.vc\n"
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("openvc.csv", payload, "text/csv")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["row_count"] == 1


# ---------- #9: row_count is the USABLE count ----------

def test_row_count_is_usable_count_not_spreadsheet_count(client) -> None:
    """Header recognized, but only 2 of 5 rows actually carry a
    firm value -> row_count should be 2, not 5."""
    payload = (
        b"name,domain\n"
        b"Real Firm,real.vc\n"
        b",garbage.example\n"            # empty firm, ignored
        b"   ,whitespace.example\n"      # whitespace-only firm
        b"Second Real,second.vc\n"
        b",\n"                            # blank line in middle
    )
    res = client.post(
        "/pipeline/sources",
        headers=_auth(),
        files={"file": ("sparse.csv", payload, "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["row_count"] == 2
