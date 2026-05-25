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





def test_full_pipeline_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        # Ensure a fresh db so the test runs from scratch.
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("03_mine_activity.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("04_mine_partner_signals.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("05_verify_and_quality.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5", cwd=REPO_ROOT)

        # --- row counts ---
        counts = _counts(db)
        assert counts["funds"] == 5
        assert counts["partners"] == 8
        assert counts["signals"] == 11
        assert counts["source_snapshots"] >= 26  # 15 fund pages + 11 signal sources
        # Batch 32: 12 matched-lead-fund rows + skeleton rows for any
        # announcement whose lead investor couldn't resolve. The
        # fixture announcements include leads not in funds_seed.csv,
        # so total >= 12; matched count is still exactly 12.
        assert counts["deal_attributions"] >= 12
        assert counts["partner_score_summaries"] == 7  # 8 partners minus Alan (no signals)
        assert counts["email_drafts"] == 10  # 5 partners x 2 variants
        assert counts["followup_drafts"] == 5
        assert counts["deck_request_responses"] == 5
        assert counts["batch_qa_reports"] >= 1

        # --- Stage 3 sector_tags persisted (batch 2 fix) ---
        c = sqlite3.connect(db)
        matched_tagged = c.execute(
            "select count(*) from deal_attributions where sector_tags is "
            "not null and lead_fund_id is not null"
        ).fetchone()[0]
        assert matched_tagged == 12, (
            f"expected 12 matched-fund deal_attributions tagged, got "
            f"{matched_tagged}"
        )
        sample_tags = json.loads(
            c.execute(
                "select sector_tags from deal_attributions where company='LedgerKit'"
            ).fetchone()[0]
        )
        assert "fintech" in sample_tags

        # --- Stage 5 verification + quality ---
        verified = c.execute(
            "select count(*) from signals where verified=1"
        ).fetchone()[0]
        assert verified == 11
        q2_plus = c.execute(
            "select count(*) from signals where signal_quality_score>=2"
        ).fetchone()[0]
        assert q2_plus == 11

        # --- Stage 6 recommended_to_send ---
        recommended = c.execute(
            "select count(*) from partner_score_summaries where recommended_to_send=1"
        ).fetchone()[0]
        assert recommended == 5, (
            f"expected 5 partners recommended, got {recommended}"
        )

        # --- batch_qa passed ---
        passed = c.execute(
            "select passed from batch_qa_reports order by report_id desc limit 1"
        ).fetchone()[0]
        assert passed == 1, "batch QA did not pass"
        c.close()

        # --- CSV shape ---
        csv_path = ws_dst / "exports" / "review_queue.csv"
        assert csv_path.exists()
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(reader.fieldnames) == 30, (
                f"expected 30 CSV columns, got {len(reader.fieldnames)}"
            )
            assert len(rows) == 5
            for row in rows:
                assert row["partner_id"]
                assert row["outreach_email_draft"]
                assert row["email_subject_line"]
                # Slice 1 cold-outreach model: every draft starts in
                # needs_review or qa_failed (when blockers are
                # present). Nothing auto-approves.
                assert row["outreach_status"] in ("needs_review", "qa_failed")

        # --- idempotency: re-run Stages 2-7, counts should not grow ---
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("03_mine_activity.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("04_mine_partner_signals.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run("05_verify_and_quality.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5", cwd=REPO_ROOT)

        new_counts = _counts(db)
        for table in ("funds", "partners", "signals", "deal_attributions",
                      "partner_score_summaries", "source_snapshots",
                      "email_drafts", "followup_drafts", "deck_request_responses"):
            assert new_counts[table] == counts[table], (
                f"idempotency broken on {table}: was {counts[table]}, now {new_counts[table]}"
            )





def test_ready_to_send_ceiling_blocks_without_approval():
    """Stage 7 must refuse >25 ready_to_send without --approve-bulk-ready."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s in ("01_aggregate_sources.py", "02_enrich_funds.py",
                  "03_mine_activity.py", "04_mine_partner_signals.py",
                  "05_verify_and_quality.py", "06_score_candidates.py"):
            extra = ("--fixtures",) if s in (
                "02_enrich_funds.py", "03_mine_activity.py", "04_mine_partner_signals.py"
            ) else ()
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # Force the ceiling low by patching the module-level constant via
        # a tiny driver script that imports + invokes Stage 7's main.
        driver = ws_dst / "_drive_stage7.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s7', {str(REPO_ROOT / 'scripts' / '07_generate_emails.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.READY_TO_SEND_DAILY_CEILING = 3\n"
            f"sys.argv = ['s7', '--workspace', {ws!r}, '--top', '5']\n"
            "raise SystemExit(m.main())\n"
        )
        res = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
        )
        assert res.returncode == 2, (
            f"expected ceiling to refuse (exit 2), got {res.returncode}\n{res.stdout}{res.stderr}"
        )
        assert "HARD CEILING" in res.stdout

        # With approval + reason it should pass.
        driver.write_text(driver.read_text().replace(
            f"['s7', '--workspace', {ws!r}, '--top', '5']",
            f"['s7', '--workspace', {ws!r}, '--top', '5', "
            f"'--approve-bulk-ready', '--reason', 'smoke test approval']",
        ))
        res = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
        )
        assert res.returncode == 0, (
            f"approved run should succeed, got {res.returncode}\n{res.stdout}{res.stderr}"
        )
        c = sqlite3.connect(db)
        note = c.execute(
            "select error_summary from runs where stage='07_generate_emails' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert note and "BULK_READY_APPROVED" in note





def test_attribution_promotion_and_bulk_reattribute_flow():
    """Slice 12 honest E2E: provisional row created by Stage 3, promoted
    via promote_provisional.py, and merged via bulk_reattribute.py.

    Drives the operator side of the new --allow-provisional flow.
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
        _run("02_enrich_funds.py", "--workspace", ws, "--fixtures", cwd=REPO_ROOT)
        _run(
            "03_mine_activity.py", "--workspace", ws,
            "--fixtures", "--allow-provisional", cwd=REPO_ROOT,
        )

        c = sqlite3.connect(db)
        provisional_funds = c.execute(
            "select fund_id, name from funds where is_provisional=1"
        ).fetchall()
        c.close()
        assert provisional_funds, (
            "Stage 3 --allow-provisional should have created provisional "
            "fund rows for fixture lead investors not in funds_seed.csv"
        )

        # --- promote one provisional fund in place ---
        prov_fund_id, prov_name = provisional_funds[0]
        _run(
            "promote_provisional.py", "--workspace", ws,
            "--fund-id", prov_fund_id,
            "--new-name", f"{prov_name} (verified)",
            "--new-domain", "verified.example",
            cwd=REPO_ROOT,
        )

        c = sqlite3.connect(db)
        row = c.execute(
            "select is_provisional, name, domain from funds where fund_id=?",
            (prov_fund_id,),
        ).fetchone()
        c.close()
        assert row[0] == 0, "is_provisional should have been cleared"
        assert row[1] == f"{prov_name} (verified)"
        assert row[2] == "verified.example"

        # Re-running on the now-real fund must REFUSE (exit 2).
        res = _run(
            "promote_provisional.py", "--workspace", ws,
            "--fund-id", prov_fund_id, cwd=REPO_ROOT, check=False,
        )
        assert res.returncode == 2, (
            f"promoting an already-real fund should refuse, got "
            f"{res.returncode}\n{res.stdout}{res.stderr}"
        )
        assert "already non-provisional" in res.stdout

        # --- bulk-reattribute a different provisional fund into a real one ---
        if len(provisional_funds) >= 2:
            src_id = provisional_funds[1][0]
            c = sqlite3.connect(db)
            dst_id = c.execute(
                "select fund_id from funds where is_provisional=0 "
                "and fund_id != ? limit 1", (prov_fund_id,),
            ).fetchone()[0]
            before = c.execute(
                "select count(*) from deal_attributions where lead_fund_id=?",
                (src_id,),
            ).fetchone()[0]
            c.close()
            assert before >= 1, (
                "the second provisional fund should have at least one deal"
            )

            # Dry-run reports the count without writing.
            res = _run(
                "bulk_reattribute.py", "--workspace", ws,
                "--from-fund-id", src_id, "--to-fund-id", dst_id,
                "--dry-run", cwd=REPO_ROOT,
            )
            assert "DRY RUN" in res.stdout
            assert f"{before} deal" in res.stdout

            c = sqlite3.connect(db)
            still_on_src = c.execute(
                "select count(*) from deal_attributions where lead_fund_id=?",
                (src_id,),
            ).fetchone()[0]
            c.close()
            assert still_on_src == before, "dry-run must not write"

            # Real run.
            _run(
                "bulk_reattribute.py", "--workspace", ws,
                "--from-fund-id", src_id, "--to-fund-id", dst_id,
                cwd=REPO_ROOT,
            )
            c = sqlite3.connect(db)
            still_on_src = c.execute(
                "select count(*) from deal_attributions where lead_fund_id=?",
                (src_id,),
            ).fetchone()[0]
            moved = c.execute(
                "select count(*) from deal_attributions where lead_fund_id=? "
                "and match_status='confirmed' and matched_by='manual'",
                (dst_id,),
            ).fetchone()[0]
            c.close()
            assert still_on_src == 0
            assert moved >= before


def test_db_integrity_invariants():
    """DB-level guarantees that are easy to assert and easy to regress:
      - FK enforcement is ON (orphan inserts rejected).
      - Hot-path indexes exist.
      - source_snapshots UNIQUE (source_url, content_hash) holds.
      - Stage 6 invalidates orphan partner_score_summaries when a partner
        loses all qualifying signals (Batch 1 fix; previously only verified
        indirectly via row counts in the full pipeline).
      - upsert() refuses non-PK pk_cols.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
            ("06_score_candidates.py", ()),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        c.execute("PRAGMA foreign_keys = ON")

        # --- hot-path indexes exist (delete-by-partner, send_now_priority
        # ordering, verified+quality scans, etc.) ---
        index_rows = c.execute(
            "select name, tbl_name from sqlite_master where type='index' "
            "and (name like 'ix_%' or name like 'ux_%')"
        ).fetchall()
        index_names = {r[0] for r in index_rows}
        for needed in (
            "ix_signals_partner_id",
            "ix_signals_verified_quality",
            "ix_pss_send_now_priority",
            "ix_email_drafts_partner_id",
            "ix_runs_workspace_stage_started",
            "ux_source_snapshots_url_hash",
        ):
            assert needed in index_names, (
                f"missing index {needed!r}; got {sorted(index_names)}"
            )

        # --- FK enforcement is active: orphan insert rejected ---
        try:
            c.execute(
                "insert into partner_score_summaries (partner_id) "
                "values ('totally-not-a-real-partner-id')"
            )
            c.commit()
            assert False, "expected FK constraint violation on orphan insert"
        except sqlite3.IntegrityError:
            pass  # expected

        # --- UNIQUE (source_url, content_hash) enforced ---
        row = c.execute(
            "select source_url, content_hash from source_snapshots "
            "where content_hash is not null limit 1"
        ).fetchone()
        assert row is not None, "no source_snapshots to test UNIQUE against"
        url, h = row
        try:
            c.execute(
                "insert into source_snapshots (source_url, fetched_at, "
                "content_hash) values (?, datetime('now'), ?)",
                (url, h),
            )
            c.commit()
            assert False, "expected UNIQUE constraint violation on duplicate hash"
        except sqlite3.IntegrityError:
            pass  # expected

        # --- Batch 1: orphan partner_score_summaries gets invalidated when
        # the partner loses all qualifying signals on the next Stage 6 run.
        # Pick a partner who currently HAS a summary, blank their signals,
        # re-run Stage 6, expect their summary row gone.
        pid_row = c.execute(
            "select partner_id from partner_score_summaries limit 1"
        ).fetchone()
        assert pid_row is not None
        target_pid = pid_row[0]
        c.execute(
            "delete from signals where partner_id = ?", (target_pid,)
        )
        c.commit()
        c.close()

        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        remaining = c.execute(
            "select count(*) from partner_score_summaries where partner_id = ?",
            (target_pid,),
        ).fetchone()[0]
        assert remaining == 0, (
            f"partner_score_summaries for {target_pid!r} not invalidated "
            f"after their signals were removed (Batch 1 regression)"
        )
        # Per-axis scores for the same partner are also cleaned up.
        score_count = c.execute(
            "select count(*) from scores where partner_id = ?", (target_pid,),
        ).fetchone()[0]
        assert score_count == 0, (
            f"scores rows for {target_pid!r} not cleaned up alongside summary"
        )
        c.close()

        # --- upsert() guard rejects pk_cols that aren't the actual PK ---
        from core.db import get_engine, upsert, funds
        engine = get_engine(f"sqlite:///{db}")
        with engine.begin() as conn:
            try:
                upsert(conn, funds, ["name"], {"name": "Should Fail"})
                assert False, "upsert() should refuse non-PK pk_cols"
            except ValueError as exc:
                assert "primary_key" in str(exc)





def test_batch16_doctor_invariant_for_orphan_summary_via_drift():
    """Inventory #907 + #503: doctor surfaces an orphan summary when the
    partners row is deleted out from under it (simulating an older DB
    without FK enforcement). FK enforcement makes this hard to trigger
    in fresh DBs -- we have to temporarily disable FKs to inject the
    drift, then re-enable for the doctor read."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)

        c = sqlite3.connect(db)
        # FKs are OFF for direct sqlite3 connections by default. Inject the
        # orphan -- specifically, sever partner_id without touching the
        # cascading children so the summary survives the delete.
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute(
            "update partner_score_summaries set partner_id = ? "
            "where partner_id = (select partner_id from partner_score_summaries limit 1)",
            ("orphan-fake-partner-id",),
        )
        c.commit()
        c.close()

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        payload = json.loads(res.stdout)
        assert any(
            "partner_score_summaries" in e and "orphan" in e
            for e in payload["errors"]
        ), f"expected orphan summary finding; got {payload['errors']}"





def test_doctor_clean_fixture_then_catches_injected_drift():
    """Batch 13: doctor.py reports clean on the fresh fixture pipeline,
    then surfaces specific findings when DB invariants are perturbed."""
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

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Clean run.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, (
            f"doctor should exit 0 on clean fixture; got {res.returncode}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        assert "ERRORS" not in res.stdout
        assert "WARNINGS" not in res.stdout

        # Inject: out-of-range axis score.
        c = sqlite3.connect(db)
        c.execute(
            "update scores set score = 99.0 where rowid = "
            "(select rowid from scores limit 1)"
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        payload = json.loads(res.stdout)
        assert any("outside [0, 10]" in e for e in payload["errors"])

        # Inject: future-dated signal.
        c = sqlite3.connect(db)
        c.execute(
            "update signals set quote_date = '2099-01-01' where signal_id = "
            "(select signal_id from signals limit 1)"
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        payload = json.loads(res.stdout)
        assert any("future" in e for e in payload["errors"])

        # Inject: unverified signal with quality (drift from Batch 11 fix).
        c = sqlite3.connect(db)
        c.execute(
            "update signals set verified=0, signal_quality_score=3 "
            "where signal_id = (select signal_id from signals limit 1)"
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "doctor.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        payload = json.loads(res.stdout)
        assert any("unverified" in e for e in payload["errors"])
