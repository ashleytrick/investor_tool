"""End-to-end tests for the Apollo export + import workflow (Slice 2)."""
from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import REPO_ROOT, _run, _run_pipeline_through_stage_6


def _export(ws: str, *extra: str) -> Path:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "export_partners_for_apollo.py"),
         "--workspace", ws, *extra],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    return Path(ws) / "exports" / "partners_for_apollo.csv"


def _import(ws: str, csv_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "import_partner_emails_apollo.py"),
         "--workspace", ws, "--from-csv", str(csv_path), *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_export_lists_every_known_partner():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        csv_path = _export(ws)
        assert csv_path.exists()
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) > 0
        # Required columns present.
        assert set(rows[0].keys()) == {
            "partner_id", "name", "fund_name", "fund_domain",
            "linkedin_url", "current_email",
        }
        # Fixture partners have no emails seeded.
        assert all(r["current_email"] == "" for r in rows)


def test_export_missing_only_skips_partners_with_email():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        # Seed one partner with an email so --missing-only skips them.
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        c.execute(
            "update partners set email='existing@test.example' "
            "where partner_id = (select partner_id from partners limit 1)"
        )
        c.commit()
        c.close()
        # Both exports write the same path -- read the full export
        # BEFORE running missing-only (otherwise the second run
        # overwrites the first).
        full = _export(ws)
        with full.open(encoding="utf-8") as f:
            full_rows = list(csv.DictReader(f))
        missing = _export(ws, "--missing-only")
        with missing.open(encoding="utf-8") as f:
            missing_rows = list(csv.DictReader(f))
        assert len(missing_rows) == len(full_rows) - 1


def test_import_writes_fresh_emails():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        c = sqlite3.connect(db)
        pids = [r[0] for r in c.execute(
            "select partner_id from partners limit 2"
        )]
        c.close()
        # Build a tiny import CSV.
        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            "partner_id,email\n"
            f"{pids[0]},alice@northbeam.example\n"
            f"{pids[1]},bob@tidewater.example\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv)
        assert res.returncode == 0, res.stdout + res.stderr
        # Both emails landed.
        c = sqlite3.connect(db)
        emails = dict(c.execute(
            "select partner_id, email from partners where partner_id in (?, ?)",
            (pids[0], pids[1]),
        ).fetchall())
        c.close()
        assert emails[pids[0]] == "alice@northbeam.example"
        assert emails[pids[1]] == "bob@tidewater.example"


def test_import_conflict_refuses_without_overwrite():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        c = sqlite3.connect(db)
        pid = c.execute("select partner_id from partners limit 1").fetchone()[0]
        c.execute(
            "update partners set email='existing@old.example' where partner_id = ?",
            (pid,),
        )
        c.commit()
        c.close()

        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            f"partner_id,email\n{pid},new@apollo.example\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv)
        # Conflicts -> exit 2.
        assert res.returncode == 2
        assert "CONFLICT" in res.stdout

        c = sqlite3.connect(db)
        email = c.execute(
            "select email from partners where partner_id = ?", (pid,),
        ).fetchone()[0]
        c.close()
        # Existing email preserved -- the conflict didn't overwrite.
        assert email == "existing@old.example"


def test_import_overwrite_replaces_and_invalidates_approvals():
    """The Slice 1 invalidation rule: when a partner email changes
    after an approval, that approval becomes stale_after_approval.
    --overwrite triggers this automatically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        # Need a Stage 7 run + an approved draft.
        _run(
            "07_generate_emails.py", "--workspace", ws,
            "--top", "5", "--allow-example-domains", cwd=REPO_ROOT,
        )
        # Set an email on the partner (so the draft can be approved).
        c = sqlite3.connect(db)
        draft_id, pid = c.execute(
            "select draft_id, partner_id from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        c.execute(
            "update partners set email='old@incumbent.example', "
            "email_verification_status='valid' where partner_id = ?",
            (pid,),
        )
        c.commit()
        c.close()
        # Approve the draft. --allow-example-domains so the approval
        # gate (Finding 2) accepts the fixture's .example data.
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "approve_draft.py"),
             "--workspace", ws, "--draft-id", str(draft_id),
             "--allow-example-domains"],
            check=True, capture_output=True,
            env={**os.environ, "USER": "tester"}, timeout=60,
        )
        # Now import a new email with --overwrite.
        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            f"partner_id,email\n{pid},updated@apollo.example\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv, "--overwrite")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "OVERWROTE" in res.stdout
        assert "approved drafts marked stale" in res.stdout

        c = sqlite3.connect(db)
        new_email = c.execute(
            "select email from partners where partner_id = ?", (pid,),
        ).fetchone()[0]
        status = c.execute(
            "select approval_status from email_drafts where draft_id = ?",
            (draft_id,),
        ).fetchone()[0]
        # Audit row records the stale trigger.
        latest_event = c.execute(
            "select event_type, notes from draft_approvals "
            "where draft_id = ? order by event_id desc limit 1",
            (draft_id,),
        ).fetchone()
        c.close()
        assert new_email == "updated@apollo.example"
        assert status == "stale_after_approval"
        assert latest_event[0] == "stale_after_approval"
        assert "partner_email_changed" in (latest_event[1] or "")


def test_import_unknown_partner_id_is_row_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            "partner_id,email\n"
            "definitely_not_a_real_partner_id,x@y.example\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv)
        assert res.returncode == 2
        assert "unknown_partner" in res.stdout


def test_import_invalid_email_is_row_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        c = sqlite3.connect(db)
        pid = c.execute("select partner_id from partners limit 1").fetchone()[0]
        c.close()

        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            f"partner_id,email\n{pid},not an email\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv)
        assert res.returncode == 2
        assert "invalid_email" in res.stdout


def test_import_identical_email_is_silent_no_op():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        db = ws_dst / "data" / "pipeline.db"

        c = sqlite3.connect(db)
        pid = c.execute("select partner_id from partners limit 1").fetchone()[0]
        c.execute(
            "update partners set email='same@apollo.example' where partner_id = ?",
            (pid,),
        )
        c.commit()
        c.close()
        import_csv = ws_dst / "apollo.csv"
        import_csv.write_text(
            f"partner_id,email\n{pid},same@apollo.example\n",
            encoding="utf-8",
        )
        res = _import(ws, import_csv)
        # No conflict, no row error, exits clean.
        assert res.returncode == 0
        assert "no_op=1" in res.stdout
