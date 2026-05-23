"""End-to-end smoke test for the pipeline.

Runs Stages 1 through 7 against a fresh copy of clients/test_workspace in a
temporary directory, then asserts the headline invariants:
  - row counts in funds / partners / signals / deal_attributions
  - 5 partners recommended_to_send
  - CSV has 30 columns and 5 rows
  - Stage 5 verification finds all 11 fixture signals
  - Stage 3 sector_tags persisted
  - batch_qa passed
  - idempotency: re-running each stage does not grow counts

Runs entirely in stub mode (no ANTHROPIC_API_KEY needed). ~10 seconds local.

Run: uv run pytest tests/ -v
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

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / script), *args]
    env = {**os.environ, "ANTHROPIC_API_KEY": ""}  # force stub mode
    res = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=120
    )
    assert res.returncode == 0, (
        f"{script} exited {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    return res


def _counts(db: Path) -> dict[str, int]:
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
        assert counts["deal_attributions"] == 12
        assert counts["partner_score_summaries"] == 7  # 8 partners minus Alan (no signals)
        assert counts["email_drafts"] == 10  # 5 partners x 2 variants
        assert counts["followup_drafts"] == 5
        assert counts["deck_request_responses"] == 5
        assert counts["batch_qa_reports"] >= 1

        # --- Stage 3 sector_tags persisted (batch 2 fix) ---
        c = sqlite3.connect(db)
        tagged = c.execute(
            "select count(*) from deal_attributions where sector_tags is not null"
        ).fetchone()[0]
        assert tagged == 12, f"expected all 12 deal_attributions tagged, got {tagged}"
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
                # Brief Rule 16 / Criterion 15: ready_to_send when recommended.
                assert row["outreach_status"] in ("ready_to_send", "draft")

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


def test_manual_override_skip_without_force():
    """Stage 6 must skip a partner with manual_score_override=True unless
    --force-rescore --reason is passed."""
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

        # Flip override flag on Priya.
        c = sqlite3.connect(db)
        c.execute(
            "update partner_score_summaries set manual_score_override=1, "
            "manual_override_reason='user pinned score' "
            "where partner_id='northbeam.example_priya_anand'"
        )
        c.commit()

        # Routine re-run must SKIP Priya, not overwrite her flag.
        res = _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)
        assert "manual override set" in res.stdout
        flag = c.execute(
            "select manual_score_override from partner_score_summaries "
            "where partner_id='northbeam.example_priya_anand'"
        ).fetchone()[0]
        assert flag == 1, "manual_score_override was wiped by routine run"

        # --force-rescore requires --reason.
        forced = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "06_score_candidates.py"),
             "--workspace", ws, "--force-rescore"],
            capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=60,
        )
        assert forced.returncode != 0
        assert "requires --reason" in forced.stderr

        # With --force-rescore --reason the flag still survives (it's a
        # one-time bypass, not a flag-clear).
        _run(
            "06_score_candidates.py", "--workspace", ws,
            "--force-rescore", "--reason", "smoke test forced refresh",
            cwd=REPO_ROOT,
        )
        flag2 = c.execute(
            "select manual_score_override from partner_score_summaries "
            "where partner_id='northbeam.example_priya_anand'"
        ).fetchone()[0]
        assert flag2 == 1
        c.close()
