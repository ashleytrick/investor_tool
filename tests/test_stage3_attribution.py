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





def test_batch33_ambiguous_match_queue():
    """Inventory #341/#342/#737/#738: when Stage 3's fuzzy match has two
    candidates within 5%, an ambiguous_matches row is recorded;
    resolve_ambiguous_match.py corrects both the row + the
    deal_attributions that picked the wrong fund."""
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

        # Add a second fund whose normalized name is close to an existing
        # one to force fuzzy ambiguity.
        from core.ids import fund_id_for
        c = sqlite3.connect(db)
        decoy_fund_id = fund_id_for("northbeam-east.example")
        c.execute(
            "insert into funds (fund_id, name, domain, is_active, "
            "is_provisional, last_updated) values (?, ?, ?, 1, 0, "
            "datetime('now'))",
            (decoy_fund_id, "Northbeam East Capital",
             "northbeam-east.example"),
        )
        c.commit()
        c.close()

        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        # The fixture mentions "Northbeam Capital" multiple times. With
        # "Northbeam East Capital" now in funds, fuzzy scoring should
        # produce at least one ambiguous_matches row.
        n_amb = c.execute(
            "select count(*) from ambiguous_matches where entity_type='fund'"
        ).fetchone()[0]
        c.close()
        assert n_amb >= 1, (
            f"expected ambiguous_matches row after fuzzy collision; got {n_amb}"
        )

        # list_ambiguous_matches --json exposes them.
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "list_ambiguous_matches.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        rows = json.loads(res.stdout)
        assert rows and len(rows[0]["candidates"]) >= 2

        # Resolve one ambiguous match.
        match_id = rows[0]["match_id"]
        # Pick the "real" Northbeam id (not the decoy) explicitly.
        c = sqlite3.connect(db)
        real_id = c.execute(
            "select fund_id from funds where domain='northbeam.example'"
        ).fetchone()[0]
        c.close()
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "resolve_ambiguous_match.py"),
             "--workspace", ws, "--match-id", str(match_id),
             "--resolved-id", real_id, "--note", "real Northbeam, not East",
             "--resolved-by", "smoke-test"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        resolved = c.execute(
            "select resolved_id, resolved_by, resolution_note "
            "from ambiguous_matches where match_id=?", (match_id,),
        ).fetchone()
        c.close()
        assert resolved[0] == real_id
        assert resolved[1] == "smoke-test"
        assert "real Northbeam" in resolved[2]

        # Resolving to a non-existent id refuses.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "resolve_ambiguous_match.py"),
             "--workspace", ws, "--match-id", str(match_id),
             "--resolved-id", "nonexistent.example",
             "--note", "should fail"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2





def test_batch32_stage3_provisional_and_raw_names():
    """Inventory #741/#742/#744/#745: Stage 3 records raw names for
    unmatched leads/partners, --allow-provisional creates provisional
    funds + partners, list_unmatched_attributions surfaces them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        # Run Stages 1 + 2 (so partners table is populated for the
        # match path), then Stage 3 WITHOUT --allow-provisional. The
        # fixture announcements name funds + partners that aren't in
        # funds_seed.csv; raw names should be persisted but no provisional
        # rows created.
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        baseline_funds = c.execute("select count(*) from funds").fetchone()[0]
        baseline_partners = c.execute(
            "select count(*) from partners"
        ).fetchone()[0]
        c.close()

        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        # Every row should have raw_lead_investor recorded (even matched ones).
        n_with_raw = c.execute(
            "select count(*) from deal_attributions "
            "where raw_lead_investor is not null"
        ).fetchone()[0]
        assert n_with_raw > 0, (
            "raw_lead_investor must be recorded on every Stage 3 row"
        )
        # Skeleton rows for unmatched leads exist (lead_fund_id NULL with
        # a raw name on file).
        n_skeleton = c.execute(
            "select count(*) from deal_attributions "
            "where lead_fund_id is null and raw_lead_investor is not null"
        ).fetchone()[0]
        assert n_skeleton > 0, (
            "expected skeleton rows for unmatched leads in fixture"
        )
        # No provisional funds/partners created without the flag.
        n_prov_funds = c.execute(
            "select count(*) from funds where is_provisional=1"
        ).fetchone()[0]
        n_prov_partners = c.execute(
            "select count(*) from partners where is_provisional=1"
        ).fetchone()[0]
        assert n_prov_funds == 0
        assert n_prov_partners == 0
        c.close()

        # Now re-run WITH --allow-provisional. Provisional funds (and
        # partners with named funds) should appear.
        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             "--allow-provisional", cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        n_prov_funds = c.execute(
            "select count(*) from funds where is_provisional=1"
        ).fetchone()[0]
        # Some skeleton rows should now have lead_fund_id populated.
        n_resolved_via_prov = c.execute(
            "select count(*) from deal_attributions d "
            "join funds f on f.fund_id = d.lead_fund_id "
            "where f.is_provisional=1"
        ).fetchone()[0]
        c.close()
        assert n_prov_funds > 0, (
            f"--allow-provisional should create provisional fund rows; "
            f"got {n_prov_funds}"
        )
        assert n_resolved_via_prov > 0

        # list_unmatched_attributions --json works.
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "list_unmatched_attributions.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        payload = json.loads(res.stdout)
        assert "unmatched_funds" in payload
        assert "unmatched_partners" in payload





def test_batch27_stage3_unresolved_partner_audit():
    """Inventory #345: when Stage 3's LLM names a partner the local DB
    doesn't know about, the partner-level attribution is correctly
    dropped (Stage 2 will backfill), but the run must log a note
    naming the missed attribution so the operator sees it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        # Reset Stage 1 + 2 so partners exist for fixture funds, then
        # erase one partner so the Stage 3 announcement naming them
        # becomes unresolvable.
        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)
        # Drop a partner that Stage 3's fixture references; the announcements
        # fixture lists named partners, so removing them forces the
        # unresolved-partner audit path.
        c = sqlite3.connect(db)
        c.execute("delete from partners")
        c.commit()
        c.close()

        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        note = c.execute(
            "select error_summary from runs "
            "where stage='03_mine_activity' order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        # The note may be empty if no announcements named known partners;
        # but at least one of the fixture announcements should have named
        # somebody, so the note text should mention "unresolved partner".
        assert note and "unresolved partner" in note, (
            f"expected unresolved partner note; got {note!r}"
        )
