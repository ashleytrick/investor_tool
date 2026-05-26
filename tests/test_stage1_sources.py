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


# ---------- CSV column aliasing (OpenVC + canonical headers) ------------


def _load_stage1_module():
    """The script isn't packaged so import the module from its path
    for the direct-call tests below."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "stage1", REPO_ROOT / "scripts" / "01_aggregate_sources.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_csv_parser_accepts_canonical_name_domain_headers() -> None:
    """The baseline contract -- a CSV with lowercase `name` + `domain`
    columns still works. Aliasing is additive; the original shape
    cannot break."""
    mod = _load_stage1_module()
    rows = mod._parse_csv_text(
        "name,domain\n"
        "Northbeam Capital,northbeam.example\n"
        "Tidewater Ventures,tidewater.example\n"
    )
    assert rows == [
        {"name": "Northbeam Capital", "domain": "northbeam.example"},
        {"name": "Tidewater Ventures", "domain": "tidewater.example"},
    ]


def test_csv_parser_handles_openvc_headers() -> None:
    """Real OpenVC export headers are `Investor name` + `Website`,
    with `Website` carrying a full URL (not a bare domain). The
    aliasing layer maps both + normalize_domain extracts the host
    so Stage 1's downstream stays in canonical shape."""
    mod = _load_stage1_module()
    sample = (
        "Investor name,Website,Global HQ,Investor type\n"
        "[sic] Ventures,https://www.sicstudio.org/ventures/ventures,"
        "\"San Francisco, CA\",VC\n"
        "1 4 All Group,https://1-4-all.group/,\"Dubai, UAE\","
        "Angel network\n"
    )
    rows = mod._parse_csv_text(sample)
    assert len(rows) == 2
    assert rows[0]["name"] == "[sic] Ventures"
    # Full URL -> bare domain via normalize_domain.
    assert rows[0]["domain"] == "sicstudio.org"
    assert rows[1]["domain"] == "1-4-all.group"


def test_csv_parser_skips_rows_missing_either_field() -> None:
    """A row that lacks name OR domain is dropped silently --
    matches the existing pre-aliasing behavior."""
    mod = _load_stage1_module()
    rows = mod._parse_csv_text(
        "investor name,website\n"
        ",https://noname.example\n"
        "Has Name,\n"
        "Real Fund,https://real.example\n"
    )
    assert rows == [{"name": "Real Fund", "domain": "real.example"}]


def test_csv_parser_strips_utf8_bom() -> None:
    """Excel-exported CSVs commonly include a UTF-8 BOM as the first
    byte. csv.DictReader sees it as part of the first header name,
    breaking the canonical-header path. Aliasing must work past BOM."""
    mod = _load_stage1_module()
    bom = "﻿"
    rows = mod._parse_csv_text(
        f"{bom}name,domain\nAcme,acme.example\n"
    )
    assert rows == [{"name": "Acme", "domain": "acme.example"}]


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
