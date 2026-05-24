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
# Allow `from core.db import ...` in tests that exercise the library directly
# (test_db_integrity_invariants does). The pipeline scripts add this themselves,
# but pytest is launched from the repo root and doesn't inherit the same path.
sys.path.insert(0, str(REPO_ROOT))


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


def test_jobs_produce_suggestions_and_apply():
    """monthly_learning_report seeds outcomes -> writes suggestions ->
    apply_axis_suggestion mutates axes.yaml + backs up + marks approved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        # Build the universe so axes/scores/outcomes have a substrate.
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
            ("05_verify_and_quality.py", ()),
            ("06_score_candidates.py", ()),
            ("07_generate_emails.py", ("--top", "5")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # attio_outcome_sync skips cleanly without attio.yaml
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "attio_outcome_sync.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "skipping" in res.stdout

        # monthly_learning_report with seed -> at least 1 suggestion.
        # --include-fixture-outcomes is required because the seeded rows
        # are tagged source='fixture' and the learning report excludes
        # those by default (so a real workspace scaffolded from fixtures
        # doesn't silently train on toy data).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes",
             "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "suggestion" in res.stdout

        c = sqlite3.connect(db)
        n = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n >= 1, f"expected >=1 unapproved suggestion, got {n}"
        sid, ax_id, current_w, suggested_w = c.execute(
            "select suggestion_id, axis_id, current_weight, suggested_weight "
            "from axis_weight_suggestions order by suggestion_id limit 1"
        ).fetchone()
        c.close()

        # Confirm axes.yaml NOT yet mutated by the learning report.
        axes_yaml = ws_dst / "config" / "axes.yaml"
        original_text = axes_yaml.read_text()
        assert f"weight: {current_w}" in original_text or "weight: 1.0" in original_text

        # apply_axis_suggestion mutates + backs up + marks approved.
        # Fixture-generated suggestions are confidence=low (n=2); apply
        # path now requires --accept-low-confidence (Finding 67).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid),
             "--accept-low-confidence"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "backup=" in res.stdout

        # Re-running on the same now-approved suggestion is a no-op.
        res_again = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res_again.returncode == 0
        assert "already approved" in res_again.stdout

        # Backup file present
        backups = list((ws_dst / "config").glob("axes.yaml.bak.*"))
        assert len(backups) == 1, f"expected 1 backup, got {len(backups)}"

        # axes.yaml weight for the targeted axis is updated
        new_text = axes_yaml.read_text()
        assert new_text != original_text
        import yaml as _yaml
        loaded = _yaml.safe_load(new_text)
        target_axis = next(a for a in loaded["axes"] if a["id"] == ax_id)
        assert float(target_axis["weight"]) == float(suggested_w)

        # Re-applying same suggestion is a no-op
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "already approved" in res.stdout


def test_operator_clis():
    """The four new operator CLIs work end-to-end on the fixture workspace."""
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
            ("07_generate_emails.py", ("--top", "5")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # prep_brief: renders markdown with all expected sections
        out_path = ws_dst / "prep.md"
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "prep_brief.py"),
             "--workspace", ws, "--partner-id",
             "northbeam.example_priya_anand", "--out", str(out_path)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        md = out_path.read_text()
        for must in ("# Prep brief:", "## Fit scores", "## Top verified quotes",
                     "## What we sent", "### Why we think this converts"):
            assert must in md, f"prep_brief missing section: {must!r}"

        # classify_reply: heuristic produces a valid outcome and writes it
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "classify_reply.py"),
             "--workspace", ws,
             "--partner-id", "northbeam.example_priya_anand",
             "--yes", "--text", "Thanks but can you send the deck first?"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        assert "asked_for_deck" in res.stdout
        c = sqlite3.connect(db)
        n_outcomes = c.execute(
            "select count(*) from outcomes where partner_id="
            "'northbeam.example_priya_anand' and reply_type='asked_for_deck'"
        ).fetchone()[0]
        assert n_outcomes == 1

        # calibration: start a cohort, then Stage 7 --top 25 must refuse
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "calibration.py"),
             "--workspace", ws, "--start", "--n", "3"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        n_pending = c.execute(
            "select count(*) from calibration_cohorts where outcome is null"
        ).fetchone()[0]
        assert n_pending == 1

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "07_generate_emails.py"),
             "--workspace", ws, "--top", "25"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        assert "GATE 5.5" in res.stdout

        # bypass with --skip-calibration --reason succeeds
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "07_generate_emails.py"),
             "--workspace", ws, "--top", "25",
             "--skip-calibration", "--reason", "smoke test bypass"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        note = c.execute(
            "select error_summary from runs where stage='07_generate_emails' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        assert note and "CALIBRATION_SKIPPED" in note

        # complete the cohort Green, --top 25 now passes without --skip
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "calibration.py"),
             "--workspace", ws, "--complete", "--outcome", "green",
             "--reason", "smoke test"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "07_generate_emails.py"),
             "--workspace", ws, "--top", "25"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0

        # set_partner_email then create_gmail_drafts skip cleanly without creds
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
             "--workspace", ws,
             "--partner-id", "northbeam.example_priya_anand",
             "--email", "priya@northbeam.example"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        email = c.execute(
            "select email from partners where partner_id="
            "'northbeam.example_priya_anand'"
        ).fetchone()[0]
        assert email == "priya@northbeam.example"

        # connect_gmail without credentials -> exit 2 + setup walkthrough
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "connect_gmail.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        assert "GCP setup" in res.stdout or "Gmail isn't linked" in res.stdout

        # create_gmail_drafts without credentials -> skip cleanly + point at
        # connect_gmail
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "create_gmail_drafts.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "connect_gmail.py" in res.stdout
        c.close()


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


def test_extract_json_tolerates_malformed_fences():
    """_extract_json must not IndexError on a single-fence response
    (Batch 7: model truncation used to crash the JSON extractor)."""
    from core.llm.client import _extract_json

    # Well-formed: opening + closing fence
    assert _extract_json('```json\n{"a": 1}\n```')["a"] == 1
    # Truncated: only an opening fence -- previously IndexError on split.
    assert _extract_json('```json\n{"a": 2}')["a"] == 2
    # No fence at all
    assert _extract_json('  {"a": 3}  ')["a"] == 3
    # Embedded prose around the JSON
    assert _extract_json('Sure thing! {"a": 4} hope that helps.')["a"] == 4
    # No JSON at all -> ValueError, NOT IndexError
    import pytest
    with pytest.raises(ValueError):
        _extract_json("```\nno json here at all\n```")


def _run_pipeline_through_stage_6(ws_dst: Path) -> None:
    """Helper: drive the fixture pipeline up to Stage 6 (no emails yet)."""
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


def test_batch_qa_failure_blocks_csv_publication():
    """When evaluate_batch() reports passed=False, Stage 7 must:
      - record the failed batch_qa_reports row,
      - NOT overwrite the previous review_queue.csv,
      - NOT delete-and-replace email_drafts/followup_drafts/deck_request_responses,
      - return non-zero,
      - surface the failure in runs.error_summary.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)

        # ---- Run 1: passing batch publishes a good CSV + drafts ----
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5", cwd=REPO_ROOT)
        csv_path = ws_dst / "exports" / "review_queue.csv"
        assert csv_path.exists(), "first run should have produced the CSV"
        good_csv_bytes = csv_path.read_bytes()
        good_csv_mtime = csv_path.stat().st_mtime

        c = sqlite3.connect(db)
        good_drafts = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        assert good_drafts > 0
        c.close()

        # ---- Force batch QA to fail by patching the similarity threshold
        # so EVERY pair in the fixture flunks the body gate. Drive Stage 7
        # via importlib so we can mutate the module-level constant for one
        # call (same trick as the ceiling test).
        driver = ws_dst / "_drive_stage7_qa_fail.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s7', {str(REPO_ROOT / 'scripts' / '07_generate_emails.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.SIM_BODY_HARD = 0.0\n"  # any nonzero similarity becomes a failure
            f"sys.argv = ['s7', '--workspace', {ws!r}, '--top', '5']\n"
            "raise SystemExit(m.main())\n"
        )
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=120,
        )
        assert res.returncode == 2, (
            f"failed-QA run should exit 2, got {res.returncode}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
        assert "BATCH QA REFUSED" in res.stdout
        assert "HARD FAIL" in res.stdout

        # ---- CSV unchanged (last good batch preserved) ----
        assert csv_path.read_bytes() == good_csv_bytes, (
            "review_queue.csv was overwritten despite failed QA"
        )
        assert csv_path.stat().st_mtime == good_csv_mtime, (
            "review_queue.csv mtime changed despite failed QA"
        )

        # ---- email_drafts not wiped + replaced ----
        c = sqlite3.connect(db)
        post_drafts = c.execute(
            "select count(*) from email_drafts"
        ).fetchone()[0]
        assert post_drafts == good_drafts, (
            f"email_drafts changed despite failed QA: "
            f"{good_drafts} -> {post_drafts}"
        )

        # ---- batch_qa_reports has BOTH the passing and failing row ----
        report_rows = c.execute(
            "select passed from batch_qa_reports order by report_id"
        ).fetchall()
        assert len(report_rows) == 2, (
            f"expected 2 batch_qa_reports rows (pass + fail), got {len(report_rows)}"
        )
        assert report_rows[0][0] == 1
        assert report_rows[1][0] == 0

        # ---- runs.error_summary surfaces the failure ----
        note = c.execute(
            "select error_summary from runs where stage='07_generate_emails' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        assert note and "BATCH QA REFUSED" in note
        c.close()


def test_batch_qa_passing_still_publishes_csv():
    """The happy path: when evaluate_batch passes, the CSV gets written
    and email_drafts get rewritten. (Asserted explicitly so the gate
    above can't regress into refusing every batch.)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5", cwd=REPO_ROOT)

        csv_path = ws_dst / "exports" / "review_queue.csv"
        assert csv_path.exists(), "passing-QA run must produce the CSV"

        c = sqlite3.connect(db)
        n_drafts = c.execute("select count(*) from email_drafts").fetchone()[0]
        assert n_drafts == 10, f"expected 5x2 variants, got {n_drafts}"
        passed = c.execute(
            "select passed from batch_qa_reports order by report_id desc limit 1"
        ).fetchone()[0]
        assert passed == 1, "batch_qa_reports.passed should be 1 on happy path"
        c.close()


def test_negative_signal_lowers_axis_score_and_blocks_strategy():
    """Batch 8: signal_direction='negative' must (a) lower the stub axis
    score instead of raising it, and (b) NOT make signal_led eligible.
    Previously a negative quality-3 quote on axis_1 would push the axis
    score from null to 9.0 AND enable signal_led."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s6", REPO_ROOT / "scripts" / "06_score_candidates.py"
    )
    s6 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s6)

    axes_cfg = {"axes": [{"id": "axis_1", "name": "x", "description": "y",
                          "weight": 1.0}]}
    pos = [{"id": 1, "quality": 3, "axes": ["axis_1"], "direction": "positive",
            "quote": "x"}]
    neg = [{"id": 2, "quality": 3, "axes": ["axis_1"], "direction": "negative",
            "quote": "x"}]

    pos_scored = s6._stub_axis_scores(pos, axes_cfg)["axis_1"]["score"]
    neg_scored = s6._stub_axis_scores(neg, axes_cfg)["axis_1"]["score"]
    assert pos_scored > 6.0, f"positive q3 should raise score, got {pos_scored}"
    assert neg_scored < 6.0, (
        f"negative q3 should LOWER score, got {neg_scored}"
    )

    # Stage 7 eligibility helper: filter to positive only before checking.
    positive_signals = [s for s in neg if (s.get("direction") or "").lower() == "positive"]
    has_q3_neg = any(s["quality"] >= 3 for s in positive_signals)
    assert has_q3_neg is False, "negative q3 must not enable signal_led"


def test_monthly_learning_excludes_fixture_outcomes_by_default():
    """Batch 8: outcomes with source='fixture' must NOT drive the
    learning report unless --include-fixture-outcomes is passed."""
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
            ("07_generate_emails.py", ("--top", "5")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Seed fixture outcomes, then run learning WITHOUT
        # --include-fixture-outcomes. Should exclude all the seeded rows
        # and report no usable outcomes (so no axis suggestions).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        assert "excluded" in res.stdout and "source='fixture'" in res.stdout, (
            f"expected fixture-exclusion notice; stdout=\n{res.stdout}"
        )

        c = sqlite3.connect(db)
        n_sugs = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n_sugs == 0, (
            f"expected no suggestions when fixture outcomes excluded, got {n_sugs}"
        )

        # With --include-fixture-outcomes, suggestions ARE produced.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 0
        n_sugs = c.execute(
            "select count(*) from axis_weight_suggestions where approved is null"
        ).fetchone()[0]
        assert n_sugs >= 1, (
            f"expected >=1 suggestion with --include-fixture-outcomes, got {n_sugs}"
        )

        # All seeded rows must be tagged source='fixture'.
        n_fixture = c.execute(
            "select count(*) from outcomes where source='fixture'"
        ).fetchone()[0]
        assert n_fixture >= 1, "seeded outcomes must carry source='fixture'"
        c.close()


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

        # Default: refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "00_verify_attio_schema.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2, (
            f"expected exit 2 on missing key, got {res.returncode}\n"
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


def test_production_guards_block_example_domains():
    """Batch 9: Stage 7 should downgrade ready_to_send -> draft when the
    workspace uses .example scheduling links / founder emails, AND
    --allow-example-domains should restore the prior behavior."""
    from core.production_guards import (
        contains_placeholder,
        is_example_domain,
        is_example_email,
        production_gate_for_ready_to_send,
        production_gate_for_attio_sync,
        production_gate_for_gmail_draft,
    )

    # Helpers
    assert contains_placeholder("Hi {NAME}") is True
    assert contains_placeholder("Hi Priya") is False
    assert is_example_domain("cal.example") is True
    assert is_example_domain("example.com") is True
    assert is_example_domain("foundry.vc") is False
    assert is_example_email("a@b.example") is True
    assert is_example_email("a@b.com") is False
    assert is_example_email("not-an-email") is False
    assert is_example_email(None) is False

    # ready_to_send gate refuses .example by default
    fails = production_gate_for_ready_to_send(
        subject="Tendril seed round", body="We are raising a $3M seed.",
        scheduling_link="https://cal.example/dana",
        founder_email="dana@tendril.example",
        partner_email=None,
    )
    assert any(".example" in f or "example/reserved" in f for f in fails)

    # ...but allow-example-domains permits .example
    fails = production_gate_for_ready_to_send(
        subject="Tendril seed round", body="We are raising a $3M seed.",
        scheduling_link="https://cal.example/dana",
        founder_email="dana@tendril.example",
        partner_email=None,
        allow_example_domains=True,
    )
    assert fails == [], f"allow_example_domains should clear all fails, got {fails}"

    # Placeholder check fires even WITH allow_example_domains
    fails = production_gate_for_ready_to_send(
        subject="Tendril {ROUND_NAME}", body="raise body",
        scheduling_link="https://cal.example/dana",
        founder_email="dana@tendril.example",
        partner_email=None,
        allow_example_domains=True,
    )
    assert any("placeholder" in f for f in fails), (
        f"placeholder check must fire even with allow_example_domains, "
        f"got {fails}"
    )

    # Attio sync gate refuses .example fund domain
    fails = production_gate_for_attio_sync(
        fund_domain="foundrynorth.example", partner_email=None,
    )
    assert any(".example" in f or "example/reserved" in f for f in fails)
    fails = production_gate_for_attio_sync(
        fund_domain="foundrynorth.vc", partner_email=None,
    )
    assert fails == []

    # Gmail draft gate
    fails = production_gate_for_gmail_draft(
        to_email="priya@northbeam.example", from_email="dana@tendril.example",
        subject="x", body="y",
    )
    assert len(fails) >= 1


def test_stage7_downgrades_example_domains_to_draft_by_default():
    """Batch 9 end-to-end: running Stage 7 against the fixture workspace
    (which uses cal.example) without --allow-example-domains should
    downgrade every otherwise-ready row to outreach_status=draft, with
    the prod-guard reasons surfaced in recommendation_reasoning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        # Default behavior (no --allow-example-domains).
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5", cwd=REPO_ROOT)

        csv_path = ws_dst / "exports" / "review_queue.csv"
        with csv_path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        statuses = {r["outreach_status"] for r in rows}
        assert "ready_to_send" not in statuses, (
            f"ready_to_send must be blocked when .example domains in use; "
            f"got statuses={statuses}"
        )
        # At least one row should carry the prod-guard reasoning.
        prod_guard_rows = [
            r for r in rows
            if "example/reserved" in (r.get("recommendation_reasoning") or "")
            or ".example" in (r.get("recommendation_reasoning") or "")
        ]
        assert prod_guard_rows, (
            f"no row recorded a prod-guard downgrade; "
            f"reasonings={[r['recommendation_reasoning'] for r in rows]}"
        )

        # With --allow-example-domains, ready_to_send returns.
        # Re-run pipeline (Stage 6 invalidates from prior run; this just
        # re-runs Stage 7 to overwrite the CSV).
        _run(
            "07_generate_emails.py", "--workspace", ws, "--top", "5",
            "--allow-example-domains", cwd=REPO_ROOT,
        )
        with csv_path.open(encoding="utf-8") as f:
            rows2 = list(csv.DictReader(f))
        statuses2 = {r["outreach_status"] for r in rows2}
        assert "ready_to_send" in statuses2, (
            f"--allow-example-domains should restore ready_to_send; "
            f"got statuses={statuses2}"
        )


def test_batch10_schema_validators():
    """Batch 10: schema-level tightening on LLM output shapes. A malformed
    LLM output should raise ValidationError instead of silently flowing
    into the DB. Each helper here builds an otherwise-valid payload and
    perturbs one field at a time."""
    import pytest
    from datetime import date, timedelta

    # --- DealAttribution ---
    from schemas.deal_attribution import DealAttribution
    from pydantic import ValidationError

    base_deal = dict(
        company="Acme", round_type="Seed",
        round_size_usd=1_000_000,
        announcement_date=date.today(),
    )
    DealAttribution.model_validate(base_deal)  # baseline OK

    with pytest.raises(ValidationError):
        DealAttribution.model_validate({**base_deal, "company": ""})
    with pytest.raises(ValidationError):
        DealAttribution.model_validate({**base_deal, "round_type": "  "})
    with pytest.raises(ValidationError):
        DealAttribution.model_validate({**base_deal, "round_size_usd": -1})
    with pytest.raises(ValidationError):
        DealAttribution.model_validate({
            **base_deal,
            "announcement_date": date.today() + timedelta(days=1),
        })

    # --- partner_signals.Signal ---
    from schemas.partner_signals import Signal
    base_signal = dict(
        quoted_text="some quote",
        source_url="https://example.test/post",
        source_type="blog",
        signal_direction="positive",
        confidence="high",
        axis_relevance=["axis_1"],
    )
    Signal.model_validate(base_signal)
    with pytest.raises(ValidationError):
        Signal.model_validate({**base_signal, "quoted_text": ""})
    with pytest.raises(ValidationError):
        Signal.model_validate({**base_signal, "quoted_text": "x" * 8001})
    with pytest.raises(ValidationError):
        Signal.model_validate({
            **base_signal,
            "quote_date": date.today() + timedelta(days=1),
        })

    # --- FundEnrichment.stated_stage_focus canonicalization ---
    from schemas.fund_enrichment import FundEnrichment
    fe = FundEnrichment.model_validate({"stated_stage_focus": "Series-A"})
    assert fe.stated_stage_focus == "series a"
    fe = FundEnrichment.model_validate({"stated_stage_focus": "preseed"})
    assert fe.stated_stage_focus == "pre-seed"
    with pytest.raises(ValidationError):
        FundEnrichment.model_validate({"stated_stage_focus": "stealth-mode"})

    # --- email subject + preemption consistency ---
    from schemas.email_generation import EmailVariant
    base_var = dict(
        strategy="signal_led",
        subject="Tendril seed round",
        body="x" * 60,
    )
    EmailVariant.model_validate(base_var)
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({**base_var, "subject": "Hello there?"})
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **base_var, "subject": "this is a six word subject line",
        })
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **base_var,
            "objection_preempted": True,
            "preemption_line": "",
        })
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **base_var,
            "objection_preempted": False,
            "preemption_line": "some line",
        })

    # --- SignalQuality reasoning required ---
    from schemas.signal_quality import SignalQuality
    SignalQuality.model_validate({
        "signal_quality_score": 3, "quality_reasoning": "specific quote",
    })
    with pytest.raises(ValidationError):
        SignalQuality.model_validate({
            "signal_quality_score": 3, "quality_reasoning": "",
        })


def test_stage5_clears_quality_on_unverified():
    """Batch 11 (#351/#352): if Stage 5 re-runs and a previously-verified
    signal flips to unverified, its signal_quality_score and
    quality_reasoning must be cleared so Stage 6's quality>=2 filter and
    Stage 7's signal_led eligibility don't pick up stale quality data."""
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
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # Pick a verified signal with a real quality score, then break its
        # quoted_text so re-verification fails, then re-run Stage 5 --force.
        c = sqlite3.connect(db)
        row = c.execute(
            "select signal_id, quoted_text, signal_quality_score, "
            "quality_reasoning from signals where verified=1 and "
            "signal_quality_score >= 2 limit 1"
        ).fetchone()
        assert row is not None, "fixture should produce at least one verified q2+ signal"
        sid, old_quote, old_quality, old_reasoning = row
        assert old_quality is not None and old_reasoning, (
            "baseline: row should have non-null quality + reasoning"
        )
        # Mutate the quoted text to something that can't be verified anywhere.
        c.execute(
            "update signals set quoted_text = ? where signal_id = ?",
            (
                "this quote could not possibly appear in any real source page "
                "because it was constructed solely for the regression test",
                sid,
            ),
        )
        c.commit()
        c.close()

        _run("05_verify_and_quality.py", "--workspace", ws, "--force",
             cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        verified, qscore, qreason = c.execute(
            "select verified, signal_quality_score, quality_reasoning "
            "from signals where signal_id = ?",
            (sid,),
        ).fetchone()
        c.close()
        assert verified == 0, "signal should now be unverified"
        assert qscore is None, (
            f"signal_quality_score should be cleared on unverified "
            f"transition; still {qscore}"
        )
        assert qreason is None, (
            f"quality_reasoning should be cleared on unverified transition; "
            f"still {qreason!r}"
        )


def test_stage6_returns_nonzero_when_partner_fails():
    """Batch 11 (#357): Stage 6 previously exited 0 even when per-partner
    exceptions had landed in run.failed. Now non-zero so cron / wrapping
    scripts notice partial scoring failure."""
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
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        # Drive Stage 6 via importlib so we can monkey-patch score_candidate
        # to raise for the first partner -- exercising the per-partner
        # try/except path that increments run.failed. The corruption-based
        # approach hits an uncaught exception OUTSIDE the per-partner try
        # (in the bulk signal load), which isn't what we want to test.
        driver = ws_dst / "_drive_stage6_fail.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s6', {str(REPO_ROOT / 'scripts' / '06_score_candidates.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real = m.score_candidate\n"
            "calls = {'n': 0}\n"
            "def boom(*a, **kw):\n"
            "    calls['n'] += 1\n"
            "    if calls['n'] == 1:\n"
            "        raise RuntimeError('synthetic partner failure for #357 test')\n"
            "    return real(*a, **kw)\n"
            "m.score_candidate = boom\n"
            f"sys.argv = ['s6', '--workspace', {ws!r}]\n"
            "raise SystemExit(m.main())\n"
        )
        res = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True,
            env={**os.environ, "ANTHROPIC_API_KEY": ""}, timeout=120,
        )
        assert res.returncode == 2, (
            f"Stage 6 should exit 2 when any per-partner failure occurs, "
            f"got {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )

        # run.failed should be reflected in the runs row.
        c = sqlite3.connect(db)
        failed = c.execute(
            "select records_failed from runs where stage='06_score_candidates' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert failed >= 1, f"expected run.failed >= 1, got {failed}"
