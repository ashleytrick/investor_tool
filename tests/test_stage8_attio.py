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
from tests.conftest import REPO_ROOT, _run, _counts, _run_pipeline_through_stage_6





def test_verify_attio_schema_fails_without_key_when_attio_configured():
    """Batch 8: explicit Stage 0 run on a workspace whose attio.yaml is
    configured but whose ATTIO_API_KEY is missing must NOT silently
    exit 0 -- the operator who ran schema verification expected a real
    check. --allow-skip restores prior cron-friendly behavior."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        # Drop a minimal attio.yaml so the code path reaches the key check
        # but without enough config to actually call Attio.
        (ws_dst / "config" / "attio.yaml").write_text(
            "attio:\n"
            "  workspace_id: dummy\n"
            "  api_base: https://api.attio.com/v2\n"
            "  matching_attributes:\n"
            "    companies: domains\n"
            "    people: email_addresses\n"
            "  objects:\n"
            "    funds: companies\n"
            "    partners: people\n"
            "  fund_attributes: {}\n"
            "  partner_attributes: {}\n",
            encoding="utf-8",
        )

        ws = str(ws_dst)
        env = {**os.environ, "ATTIO_API_KEY": ""}

        # Default: refuse. Preflight maps a missing required API key
        # to REFUSED_UNSAFE (=3) consistently across stages -- the
        # stage body never runs after the refusal lands in `runs`.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "00_verify_attio_schema.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 3, (
            f"expected exit 3 (REFUSED_UNSAFE) on missing key, got "
            f"{res.returncode}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        assert "REFUSED" in res.stdout

        # --allow-skip: clean skip.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "00_verify_attio_schema.py"),
             "--workspace", ws, "--allow-skip"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "skipping" in res.stdout





def test_stage8_pushed_at_timestamps_via_driver():
    """Batch 12 (#379/#380/#381): Stage 8 should stamp pushed_to_attio_at
    on the latest recommended/alternate draft + the latest followup +
    the latest deck row when a partner sync succeeds. We can't hit a
    real Attio API in CI, so we monkey-patch AttioClient methods via
    importlib (same pattern as the QA-fail test)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)

        # Write a minimal attio.yaml so Stage 8 doesn't skip preflight.
        (ws_dst / "config" / "attio.yaml").write_text(
            "attio:\n"
            "  workspace_id: dummy\n"
            "  api_base: https://api.attio.com/v2\n"
            "  matching_attributes:\n"
            "    companies: domains\n"
            "    people: email_addresses\n"
            "  objects:\n"
            "    funds: companies\n"
            "    partners: people\n"
            "  fund_attributes: {}\n"
            "  partner_attributes: {}\n",
            encoding="utf-8",
        )

        # Drive Stage 8 with a stubbed AttioClient that fakes upsert/create/
        # update and returns canned record_ids. Just enough to walk through
        # the partner-sync loop so pushed_to_attio_at gets set.
        # Use a module-level monotonic counter -- the earlier fixture used
        # id(payload), but CPython id() reuses memory addresses after GC,
        # so two distinct payloads occasionally collided and the
        # partners.attio_record_id UNIQUE index rejected the second insert.
        driver = ws_dst / "_drive_stage8.py"
        driver.write_text(
            "import sys, importlib.util, itertools\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.attio_client as ac\n"
            "from core.attio_client import AttioClient\n"
            "_orig_from = AttioClient.from_workspace\n"
            "_co_counter = itertools.count()\n"
            "_per_counter = itertools.count()\n"
            "class FakeClient:\n"
            "    def upsert_record(self, obj, slug, payload):\n"
            "        return {'data': {'id': {'record_id': 'fake_co_' + str(next(_co_counter))}}}\n"
            "    def get_record(self, obj, rid):\n"
            "        return None\n"
            "    def create_record(self, obj, payload):\n"
            "        return {'data': {'id': {'record_id': 'fake_per_' + str(next(_per_counter))}}}\n"
            "    def update_record(self, obj, rid, payload):\n"
            "        return {'data': {'id': {'record_id': rid}}}\n"
            "    def attribute_slugs(self, obj):\n"
            "        return set()\n"
            "    def close(self):\n"
            "        pass\n"
            "ac.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
            # Also patch find_partner_record to always return None (create path).
            "import scripts as _s\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s8', {str(REPO_ROOT / 'scripts' / '08_sync_to_attio.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.find_partner_record = lambda *a, **kw: None\n"
            "# AttioClient.from_workspace is module-level used inside s8\n"
            "m.AttioClient.from_workspace = classmethod(lambda cls, ws: FakeClient())\n"
            f"sys.argv = ['s8', '--workspace', {ws!r}, '--top', '5', '--allow-example-domains', '--allow-fixture-mode']\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": "", "ATTIO_API_KEY": "fake-key"}
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 0, (
            f"Stage 8 with stubbed client should succeed, got {res.returncode}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )

        c = sqlite3.connect(db)
        # At least one recommended draft should have pushed_to_attio_at set.
        n_pushed = c.execute(
            "select count(*) from email_drafts "
            "where pushed_to_attio_at is not null"
        ).fetchone()[0]
        assert n_pushed >= 1, (
            f"expected >=1 email_drafts.pushed_to_attio_at populated; "
            f"got {n_pushed}"
        )
        # At least one followup + one deck row should also be stamped.
        n_followups = c.execute(
            "select count(*) from followup_drafts "
            "where pushed_to_attio_at is not null"
        ).fetchone()[0]
        n_decks = c.execute(
            "select count(*) from deck_request_responses "
            "where pushed_to_attio_at is not null"
        ).fetchone()[0]
        assert n_followups >= 1, f"followups pushed_to_attio_at not set ({n_followups})"
        assert n_decks >= 1, f"deck responses pushed_to_attio_at not set ({n_decks})"
        c.close()





def test_batch43_attio_outcome_sync_row_failure_exits_nonzero():
    """Inventory #86: attio_outcome_sync exits 2 when row-level
    exceptions occur. Drive via stubbed AttioClient that returns one
    valid record + one broken record so _bool/_option_title raise."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        # Inject Attio config so outcome_sync runs.
        (ws_dst / "config" / "attio.yaml").write_text(
            "attio:\n"
            "  workspace_id: dummy\n"
            "  api_base: https://api.attio.com/v2\n"
            "  matching_attributes:\n"
            "    companies: domains\n"
            "    people: email_addresses\n"
            "  objects:\n"
            "    funds: companies\n"
            "    partners: people\n"
            "  fund_attributes: {}\n"
            "  partner_attributes: {}\n",
            encoding="utf-8",
        )
        # Stamp attio_record_id on one partner so the sync has someone
        # to walk.
        c = sqlite3.connect(db)
        c.execute(
            "update partners set attio_record_id='fake_rec_1' "
            "where partner_id='northbeam.example_priya_anand'"
        )
        c.commit()
        c.close()

        # Driver: stub query_records_all to return a malformed values
        # dict that trips the per-row try/except (passing a non-dict
        # raises in _option_title).
        driver = ws_dst / "_drive_outcome_sync.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.attio_client as ac\n"
            "class FakeClient:\n"
            "    def query_records_all(self, slug, filt, **kw):\n"
            "        return [\n"
            "            {'id': {'record_id': 'fake_rec_1'},\n"
            "             'values': 'not-a-dict-deliberately'},\n"
            "        ]\n"
            "    def close(self): pass\n"
            "ac.AttioClient.from_workspace = classmethod(lambda cls, w: FakeClient())\n"
            "spec = importlib.util.spec_from_file_location("
            f"'o', {str(REPO_ROOT / 'jobs' / 'attio_outcome_sync.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.AttioClient.from_workspace = classmethod(lambda cls, w: FakeClient())\n"
            f"sys.argv = ['o', '--workspace', {ws!r}]\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": "",
                "ATTIO_API_KEY": "fake-key"}
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2, (
            f"outcome_sync row failure should exit 2; got {res.returncode}\n"
            f"STDOUT:\n{res.stdout[-500:]}"
        )





def test_batch29_fetch_result_final_url():
    """Inventory #325: FetchResult carries final_url alongside the
    requested url, so callers persisting provenance can record the
    canonical URL after redirects."""
    from core.http_client import FetchResult

    r = FetchResult(url="http://x", status=200, text="ok",
                    final_url="https://x.com/")
    assert r.url == "http://x"
    assert r.final_url == "https://x.com/"

    # Backward compat: final_url defaults to "" if not supplied (old
    # callers that constructed FetchResult without it don't break).
    r2 = FetchResult(url="http://y", status=200, text="ok")
    assert r2.final_url == ""

    # source_snapshots schema has the column.
    from core.db import source_snapshots
    assert "final_url" in {c.name for c in source_snapshots.columns}





def test_batch18_attio_api_base_allowlist():
    """Inventory #653/#654/#655: AttioClient refuses to send the bearer
    token to any host outside ALLOWED_API_BASE_HOSTS unless explicitly
    opted out."""
    from core.attio_client import (
        ALLOWED_API_BASE_HOSTS, AttioClient, AttioNotConfigured,
    )

    # Default (api.attio.com): permitted.
    AttioClient(api_key="fake", base_url="https://api.attio.com/v2")

    # Other host: refused unless opt-in.
    import pytest
    with pytest.raises(AttioNotConfigured) as exc_info:
        AttioClient(api_key="fake", base_url="https://evil.example/v2")
    assert "allowlist" in str(exc_info.value)

    # Opt-out flag: permitted.
    AttioClient(
        api_key="fake", base_url="https://self-hosted.attio.example/v2",
        allow_any_base_url=True,
    )

    # Empty / unparseable base: refused.
    with pytest.raises(AttioNotConfigured):
        AttioClient(api_key="fake", base_url="")

    # Allowlist baseline contract.
    assert "api.attio.com" in ALLOWED_API_BASE_HOSTS
