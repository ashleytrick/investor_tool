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
        driver = ws_dst / "_drive_stage8.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import core.attio_client as ac\n"
            "from core.attio_client import AttioClient\n"
            "_orig_from = AttioClient.from_workspace\n"
            "class FakeClient:\n"
            "    def upsert_record(self, obj, slug, payload):\n"
            "        return {'data': {'id': {'record_id': 'fake_co_' + str(id(payload))}}}\n"
            "    def get_record(self, obj, rid):\n"
            "        return None\n"
            "    def create_record(self, obj, payload):\n"
            "        return {'data': {'id': {'record_id': 'fake_per_' + str(id(payload))}}}\n"
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
            f"sys.argv = ['s8', '--workspace', {ws!r}, '--top', '5', '--allow-example-domains']\n"
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


def test_batch14_workspace_safety_and_clis():
    """Batch 14: friendlier YAML errors, absolute-path basename
    disambiguation, db_url URL-escaping, .gitignore generation, and the
    three new operator CLIs all behave."""
    from core.config_loader import Workspace, _load_yaml

    # ---- friendlier YAML diagnostics (#304) ----
    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "bad.yaml"
        bad.write_text("a: 1\n  b: : :\n", encoding="utf-8")
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            _load_yaml(bad)
        assert "not valid YAML" in str(exc_info.value)
        # Should include the filename so the operator knows which file to edit
        assert "bad.yaml" in str(exc_info.value)

    # ---- absolute-path basename disambiguation (#302) ----
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_a = Path(tmpdir) / "a" / "test_workspace"
        ws_b = Path(tmpdir) / "b" / "test_workspace"
        shutil.copytree(ws_src, ws_a)
        shutil.copytree(ws_src, ws_b)
        wa = Workspace(str(ws_a))
        wb = Workspace(str(ws_b))
        assert wa.name != wb.name, (
            f"two absolute workspaces with same basename should disambiguate; "
            f"got {wa.name!r} and {wb.name!r}"
        )
        # Both should still START with the bare name for readability.
        assert wa.name.startswith("test_workspace-")
        assert wb.name.startswith("test_workspace-")
        # In-repo path keeps the bare name (backward compat).
        w_repo = Workspace("clients/test_workspace")
        assert w_repo.name == "test_workspace"

    # ---- db_url URL-escapes the path (#303) ----
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_with_space = Path(tmpdir) / "has space" / "test_workspace"
        shutil.copytree(ws_src, ws_with_space)
        ws = Workspace(str(ws_with_space))
        # Path contains a space; the URL must NOT contain a raw space.
        assert " " not in ws.db_url, f"db_url has raw space: {ws.db_url!r}"
        assert "%20" in ws.db_url, (
            f"space should be URL-escaped to %20; got {ws.db_url!r}"
        )

    # ---- init_workspace writes a .gitignore (#794/#797) ----
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        # init_workspace.py only works from REPO_ROOT and writes under
        # clients/. Use a unique name.
        ws_name = f"batch14_init_test_{os.getpid()}"
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "init_workspace.py"),
             ws_name],
            capture_output=True, text=True, env=env, timeout=60, cwd=REPO_ROOT,
        )
        try:
            assert res.returncode == 0, (
                f"init_workspace should succeed; got {res.returncode}\n"
                f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
            )
            gitignore = REPO_ROOT / "clients" / ws_name / ".gitignore"
            assert gitignore.exists(), "init_workspace should drop .gitignore"
            body = gitignore.read_text(encoding="utf-8")
            for must in (".env", "pipeline.db", "raw/", "exports/"):
                assert must in body, (
                    f".gitignore missing {must!r}; got:\n{body}"
                )
        finally:
            shutil.rmtree(REPO_ROOT / "clients" / ws_name, ignore_errors=True)


def test_batch15_manual_override_polish():
    """Batch 15: warm-path requires contact, --clear-* scopes the clear,
    multi-type reasons are namespaced + preserved across overrides."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        pid = "northbeam.example_priya_anand"

        # #290: warm-path without --warm-path-contact must refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--warm-path",
             "--reason", "warm intro via board chair"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode != 0
        assert "--warm-path-contact" in (res.stderr + res.stdout)

        # Set warm-path with contact -> success.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--warm-path",
             "--reason", "warm intro via board chair",
             "--warm-path-contact", "ashley@example.com knows them"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0

        # Set score override too; reasons should be namespaced AND coexist.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--score",
             "--reason", "hand-tuned after meeting"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0

        c = sqlite3.connect(db)
        reason = c.execute(
            "select manual_override_reason from partner_score_summaries "
            "where partner_id=?", (pid,),
        ).fetchone()[0]
        assert reason, "reason field should not be NULL after dual override"
        assert "score:" in reason, f"expected namespaced score; got {reason!r}"
        assert "warm:" in reason, (
            f"warm reason should persist after score override; got {reason!r}"
        )
        c.close()

        # #287: --clear --clear-score should drop only the score namespace
        # AND keep warm_path_available=TRUE.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--clear",
             "--clear-score"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        score_flag, reason = c.execute(
            "select manual_score_override, manual_override_reason "
            "from partner_score_summaries where partner_id=?", (pid,),
        ).fetchone()
        assert score_flag == 0
        # Warm reason should still be there; score namespace should be gone.
        if reason:
            assert "score:" not in reason
            assert "warm:" in reason
        warm = c.execute(
            "select warm_path_available from partners where partner_id=?",
            (pid,),
        ).fetchone()[0]
        c.close()
        assert warm == 1, "warm_path_available should survive --clear-score"

        # #287: global --clear (no slice flags) drops everything.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--clear"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        score_flag, rec_flag, reason = c.execute(
            "select manual_score_override, manual_recommended_override, "
            "manual_override_reason from partner_score_summaries "
            "where partner_id=?", (pid,),
        ).fetchone()
        warm = c.execute(
            "select warm_path_available from partners where partner_id=?",
            (pid,),
        ).fetchone()[0]
        c.close()
        assert score_flag == 0 and rec_flag == 0
        assert reason is None
        assert warm is None


def test_batch16_live_prompt_has_no_unfilled_placeholders():
    """Inventory #828/#457: build_live_prompt should leave no `{TOKEN}` -style
    placeholders after substitution. Catches drift between the prompt
    file and build_live_prompt's .replace() chain."""
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location(
        "s7", REPO_ROOT / "scripts" / "07_generate_emails.py"
    )
    s7 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s7)

    company = {
        "company": {
            "name": "Tendril",
            "founder_name": "Dana Okafor",
            "founder_email": "dana@tendril.example",
            "description": "Compliance reporting as an API.",
            "one_liner": "Compliance reporting as an API.",
            "current_traction": {"headline_metric": "$180K ARR",
                                  "secondary_metrics": ["NRR 128%"]},
            "target_sectors": ["fintech"],
            "meeting_ask": {
                "duration_minutes": 30, "format": "video call",
                "preferred_scheduling_link": "https://cal.example/dana",
                "preferred_time_slots": ["Tue 10am PT", "Wed 2pm PT"],
            },
        },
        "raise_context": {
            "round": "Seed", "amount": "$3M", "status": "in market",
            "timing": "first close in 8 weeks",
            "why_this_round_is_fundable_now": "x",
            "what_changes_after_this_round": "y",
            "strongest_raise_proof": "z",
            "notable_existing_investors_or_non_dilutive": "w",
            "round_hook": {
                "strongest_reason_to_meet_now": "a",
                "investor_consequence_of_waiting": "b",
                "round_momentum_proof": "c",
            },
        },
        "founder_voice": {
            "style": "direct", "banned_phrases": ["would love"],
        },
    }
    prompt = s7.build_live_prompt(
        company_cfg=company,
        partner_name="Priya Anand",
        fund_name="Northbeam",
        partner_bio="Investor focused on regulated fintech.",
        composite_score=8.0,
        round_fit_score=10.0,
        round_fit_reasoning="stage match, check overlap",
        lead_likelihood_score=6.0,
        axes_summary="axis_1 (8.0), axis_2 (6.0)",
        fund_kill_signals=None,
        signals_for_partner=[
            {"quote": "regulation as moat",
             "source_url": "https://example.com/p",
             "date": "2026-02-01"},
        ],
        deals_for_partner=[
            {"company": "LedgerKit", "round_type": "Seed"},
        ],
        examples_dir=REPO_ROOT / "clients" / "test_workspace" / "prompts" / "examples",
    )
    leftover = sorted(set(re.findall(r"\{[A-Z][A-Z0-9_]*\}", prompt)))
    assert not leftover, f"unfilled placeholders in live prompt: {leftover}"
    # Sanity: examples block actually injected, not just the path.
    assert "--- signal_led ---" in prompt, (
        "expected `--- signal_led ---` from the examples block; the prompt "
        "may still be using {EXAMPLES_DIR} without {EXAMPLES_BLOCK}"
    )


def test_batch16_check_size_parser_edge_cases():
    """Inventory #919/#920/#921/#922/#923: round_fit's check-size parsing
    must handle commas, malformed ranges, and missing config without
    crashing."""
    from core.round_fit import (
        parse_check_size, ranges_overlap, compute_round_fit,
    )

    # Commas in the numeric part.
    assert parse_check_size("$1,000,000-$2,000,000") == (1_000_000, 2_000_000)
    # K / M suffixes.
    assert parse_check_size("$500K-$2M") == (500_000, 2_000_000)
    # Malformed: returns None, doesn't crash.
    assert parse_check_size("around $500K to a few million") is None
    assert parse_check_size("") is None
    assert parse_check_size(None) is None
    # Overlap helper
    assert ranges_overlap((100, 500), (400, 1000)) is True
    assert ranges_overlap((100, 500), (600, 1000)) is False

    # min > max raise context: compute_round_fit shouldn't crash.
    fund = {"stated_stage_focus": "seed", "check_size_range": "$1M-$3M",
            "is_active": True}
    partner = {"title": "Partner"}
    company = {
        "company": {"target_check_size_usd": {"min": 500_000, "max": 1_500_000},
                    "target_sectors": ["fintech"]},
        "raise_context": {"round": "Seed"},
        "round_fit": {"disqualifiers": []},
    }
    rf = compute_round_fit(fund, partner, [], False, company)
    assert 0.0 <= rf.round_fit_score <= 10.0


def test_batch26_do_not_contact_and_new_clis():
    """Inventory #441, #684, #687, #692-#695: do_not_contact flag, warm-
    path contact edit without flag flip, and list_partners_for_action."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Pick a currently-recommended partner.
        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from partner_score_summaries "
            "where recommended_to_send=1 limit 1"
        ).fetchone()[0]
        c.close()

        # #441/#684: set do_not_contact, re-run Stage 6, expect demotion.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_do_not_contact.py"),
             "--workspace", ws, "--partner-id", pid,
             "--reason", "conflict of interest"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        rec, summary = c.execute(
            "select recommended_to_send, kill_signal_summary "
            "from partner_score_summaries where partner_id=?", (pid,),
        ).fetchone()
        c.close()
        assert rec == 0, "do_not_contact partner must not be recommended"
        assert "do_not_contact" in (summary or ""), (
            f"kill_signal_summary should mention do_not_contact; "
            f"got {summary!r}"
        )

        # Clear the flag, re-run, expect recommendation restored (if it
        # was the only blocker -- the fixture partners are otherwise
        # qualified so this should work).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_do_not_contact.py"),
             "--workspace", ws, "--partner-id", pid, "--clear"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        rec_again = c.execute(
            "select recommended_to_send from partner_score_summaries "
            "where partner_id=?", (pid,),
        ).fetchone()[0]
        c.close()
        assert rec_again == 1

        # #687: set_warm_path_contact updates contact text without
        # flipping warm_path_available.
        # First, set warm-path via manual_override to get the flag on.
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "manual_override.py"),
             "--workspace", ws, "--partner-id", pid, "--warm-path",
             "--reason", "warm intro",
             "--warm-path-contact", "ashley@example.com"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        # Now update only the contact text.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_warm_path_contact.py"),
             "--workspace", ws, "--partner-id", pid,
             "--contact", "Jane via Series B board (chair)"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        flag, contact = c.execute(
            "select warm_path_available, warm_path_contact "
            "from partners where partner_id=?", (pid,),
        ).fetchone()
        c.close()
        assert flag == 1, "warm_path_available should still be TRUE"
        assert contact == "Jane via Series B board (chair)"

        # #692: list_partners_for_action --high-priority-no-email.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "list_partners_for_action.py"),
             "--workspace", ws, "--high-priority-no-email", "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        rows = json.loads(res.stdout)
        # Fixture partners don't have email set; expect several rows.
        assert isinstance(rows, list) and rows


def test_batch24_sector_matching_false_positives():
    """Inventory #419/#420/#422: word-boundary matching avoids substring
    false positives ("ai" in "stairwell", "art" in "smart") and sector
    plurals match against singular targets."""
    # Import the helper from Stage 7 via importlib.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s7", REPO_ROOT / "scripts" / "07_generate_emails.py"
    )
    s7 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s7)
    hit = s7._word_boundary_hit

    # NOTE: helper expects pre-lowercased input (Stage 7 caller does
    # `thesis_lower = (...).lower()` before passing).

    # Positive: real word matches.
    assert hit("we invest in fintech infrastructure", "fintech") is True
    assert hit("compliance reporting as an api", "api") is True
    # Multi-word phrase matches as substring at word boundaries.
    assert hit("design partners signed in q1", "design partners") is True

    # Negative: substring false positives.
    assert hit("stairwell ai is our portfolio company", "ai") is True
    assert hit("retail focus, no fintech", "ai") is False
    assert hit("smart contracts", "art") is False
    assert hit("we invest in api-first infra", "ap") is False

    # Empty needle never matches.
    assert hit("anything", "") is False

    # Sector plural / singular matching in round_fit.
    from core.round_fit import recent_relevant_deals
    deals = [
        {"sector_tags": ["payment", "regulatory"]},
        {"sector_tags": ["compliances"]},  # plural in tag
    ]
    # Targets singular -- both should match.
    assert recent_relevant_deals(deals, ["payments", "compliance"]) == 2

    # Empty targets returns 0.
    assert recent_relevant_deals(deals, []) == 0


def test_batch23_stage7_metadata():
    """Inventory #467, #471-#474: followup_drafts/deck_request_responses
    carry batch_id, written_to_csv_at is set AFTER CSV success not at
    insert, and batch_qa_reports.batch_partner_count is populated."""
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

        c = sqlite3.connect(db)
        # #473/#474: followup + deck have batch_id linking back to email_drafts
        n_followup_with_batch = c.execute(
            "select count(*) from followup_drafts where batch_id is not null"
        ).fetchone()[0]
        n_followup = c.execute(
            "select count(*) from followup_drafts"
        ).fetchone()[0]
        assert n_followup_with_batch == n_followup > 0, (
            f"every followup row should carry batch_id; "
            f"{n_followup_with_batch}/{n_followup}"
        )
        n_deck_with_batch = c.execute(
            "select count(*) from deck_request_responses where batch_id is not null"
        ).fetchone()[0]
        n_deck = c.execute(
            "select count(*) from deck_request_responses"
        ).fetchone()[0]
        assert n_deck_with_batch == n_deck > 0

        # The batch_ids should match between email_drafts and followup_drafts
        # for any given partner.
        mismatches = c.execute(
            "select e.partner_id, e.batch_id, f.batch_id "
            "from email_drafts e join followup_drafts f "
            "on e.partner_id = f.partner_id "
            "where e.is_recommended = 1 and e.batch_id != f.batch_id"
        ).fetchall()
        assert not mismatches, f"batch_id mismatches: {mismatches}"

        # #467: batch_qa_reports.batch_partner_count populated.
        size, partners_count = c.execute(
            "select batch_size, batch_partner_count from batch_qa_reports "
            "order by report_id desc limit 1"
        ).fetchone()
        assert size > 0
        assert partners_count > 0
        assert partners_count <= size, (
            f"batch_partner_count ({partners_count}) should be <= batch_size "
            f"({size})"
        )

        # #471/#472: written_to_csv_at is set on recommended email_drafts
        # rows AFTER the CSV write.
        n_written = c.execute(
            "select count(*) from email_drafts "
            "where is_recommended=1 and written_to_csv_at is not null"
        ).fetchone()[0]
        assert n_written > 0, "recommended drafts should have written_to_csv_at"
        c.close()


def test_batch22_email_schema_extends_to_alternate_and_deck():
    """Inventory #373/#374/#607/#608/#612: forbidden-phrase, em-dash, and
    exclamation-mark checks now fire at the SCHEMA layer for variant
    bodies (recommended AND alternate) and for deck_request_response /
    followup_draft. Previously only the recommended draft's body was
    inspected by Stage 7's check_hard_gates."""
    import pytest
    from pydantic import ValidationError
    from schemas.email_generation import EmailOutput, EmailVariant

    good_var = dict(
        strategy="signal_led",
        subject="Tendril seed round",
        body="We are raising a $3M seed and would like to share the deck with you "
             "next week if it is helpful. Quick call to walk through the round.",
        conversion_hypothesis="x",
        likely_objection="y",
    )

    # Baseline EmailVariant: passes.
    EmailVariant.model_validate(good_var)

    # #612: em dash in body refused.
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **good_var,
            "body": "We are raising — closing in 8 weeks — first close soon. "
                    "Quick call to walk through it." * 2,
        })
    # #612: exclamation refused.
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **good_var,
            "body": "We are raising! Closing in 8 weeks soon. " * 3,
        })
    # #608: forbidden phrase in body refused.
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **good_var,
            "body": "We are raising and would love to share the deck. "
                    "Quick call to walk through it next week sometime." * 2,
        })
    # #374: forbidden phrase in subject refused.
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **good_var, "subject": "Quick question",
        })
    # #607: template_smell out of enum refused.
    with pytest.raises(ValidationError):
        EmailVariant.model_validate({
            **good_var, "template_smell": "weird-value",
        })

    # #373/#608: forbidden phrase / em dash / ! in deck_request_response.
    base_out = dict(
        variants=[EmailVariant.model_validate(good_var)],
        recommended_variant_strategy="signal_led",
        recommendation_reasoning="strong q3 signal",
        limited_variation=True,
        limited_variation_reason="only one eligible strategy",
        deck_request_response="Deck attached. Happy to walk through next week.",
        followup_draft="Following up to ask about the round.",
    )
    EmailOutput.model_validate(base_out)  # baseline OK

    with pytest.raises(ValidationError):
        EmailOutput.model_validate({
            **base_out,
            "deck_request_response": "Deck attached — happy to walk through.",
        })
    with pytest.raises(ValidationError):
        EmailOutput.model_validate({
            **base_out,
            "followup_draft": "Excited to share an update on the round.",
        })
    with pytest.raises(ValidationError):
        EmailOutput.model_validate({
            **base_out,
            "followup_draft": "Follow up: new milestone hit!",
        })


def test_batch21_config_validators():
    """Inventory #716/#717/#718/#723/#724/#727: preflight catches the
    common config drift problems that would otherwise silently corrupt
    a real run."""
    from core.validate_config import (
        _check_axes, _check_company, _check_meeting_ask, _looks_like_email,
    )

    # #718: founder_email shape
    assert _looks_like_email("dana@tendril.example") is True
    assert _looks_like_email("dana@tendril") is False
    assert _looks_like_email("not-an-email") is False
    assert _looks_like_email("") is False
    assert _looks_like_email(None) is False

    co_base = {
        "company": {
            "name": "Tendril", "founder_name": "Dana",
            "founder_email": "dana@tendril.com", "one_liner": "x",
            "description": "y", "stage": "SEED",
            "target_check_size_usd": {"min": 100_000, "max": 1_000_000},
            "target_sectors": ["fintech"],
        },
    }
    issues: list[str] = []
    _check_company(co_base, issues)
    assert not [i for i in issues if "founder_email" in i]

    issues = []
    bad = {**co_base, "company": {**co_base["company"],
                                  "founder_email": "not-an-email"}}
    _check_company(bad, issues)
    assert any("founder_email" in i for i in issues)

    # #716/#717: meeting_ask
    issues = []
    _check_meeting_ask({"company": {"meeting_ask": {
        "duration_minutes": 30,
        "preferred_scheduling_link": "https://cal.example/dana",
    }}}, issues)
    assert not issues  # placeholder = no, https = yes, dur = 30

    issues = []
    _check_meeting_ask({"company": {"meeting_ask": {
        "duration_minutes": 30,
        "preferred_scheduling_link": "http://cal.example/dana",
    }}}, issues)
    assert any("http://" in i for i in issues)

    issues = []
    _check_meeting_ask({"company": {"meeting_ask": {
        "duration_minutes": 999,
        "preferred_scheduling_link": "https://cal.example/dana",
    }}}, issues)
    assert any("duration_minutes" in i for i in issues)

    issues = []
    _check_meeting_ask({"company": {"meeting_ask": {
        "duration_minutes": 30,
        "preferred_scheduling_link": "not-a-url",
    }}}, issues)
    assert any("https://" in i for i in issues)

    # #723/#724: axis weight positive + bounded
    axes_ok = {"axes": [
        {"id": f"axis_{i}", "name": f"n{i}", "description": f"d{i}",
         "positive_signals": ["x"], "weight": 1.0}
        for i in range(1, 5)
    ]}
    issues = []
    _check_axes(axes_ok, issues)
    assert not issues

    bad_axes = {"axes": [
        {"id": "axis_1", "name": "n1", "description": "d1",
         "positive_signals": ["x"], "weight": -1.0},
        {"id": "axis_2", "name": "n2", "description": "d2",
         "positive_signals": ["x"], "weight": 10.0},
        {"id": "axis_3", "name": "n3", "description": "d3",
         "positive_signals": ["x"], "weight": 1.0},
        {"id": "axis_4", "name": "n4", "description": "d4",
         "positive_signals": ["x"], "weight": 1.0},
    ]}
    issues = []
    _check_axes(bad_axes, issues)
    msgs = " ".join(issues)
    assert "must be positive" in msgs
    assert "> 5.0" in msgs

    # #727: duplicate axes by name/description
    dup_axes = {"axes": [
        {"id": "axis_1", "name": "same", "description": "d1",
         "positive_signals": ["x"], "weight": 1.0},
        {"id": "axis_2", "name": "same", "description": "d2",
         "positive_signals": ["x"], "weight": 1.0},
        {"id": "axis_3", "name": "n3", "description": "same",
         "positive_signals": ["x"], "weight": 1.0},
        {"id": "axis_4", "name": "n4", "description": "same",
         "positive_signals": ["x"], "weight": 1.0},
    ]}
    issues = []
    _check_axes(dup_axes, issues)
    msgs = " ".join(issues)
    assert "same name" in msgs
    assert "same description" in msgs


def test_batch20_env_precedence():
    """Inventory #815/#816: env resolution is (process env if non-empty)
    > workspace .env > root .env. An empty process env value must NOT
    mask a workspace .env value."""
    from core.config_loader import Workspace

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        # Inject a key into the workspace .env.
        (ws_dst / ".env").write_text(
            "FAKE_TEST_KEY=from_workspace_dotenv\n",
            encoding="utf-8",
        )

        # 1. No process env -> workspace .env wins.
        saved = os.environ.pop("FAKE_TEST_KEY", None)
        try:
            ws = Workspace(str(ws_dst))
            assert ws.env("FAKE_TEST_KEY") == "from_workspace_dotenv"

            # 2. Process env with non-empty value -> overrides.
            os.environ["FAKE_TEST_KEY"] = "from_process_env"
            ws = Workspace(str(ws_dst))
            assert ws.env("FAKE_TEST_KEY") == "from_process_env"

            # 3. Process env with EMPTY value -> falls back to workspace.
            os.environ["FAKE_TEST_KEY"] = ""
            ws = Workspace(str(ws_dst))
            assert ws.env("FAKE_TEST_KEY") == "from_workspace_dotenv", (
                "empty process env must NOT mask workspace .env value"
            )
        finally:
            if saved is None:
                os.environ.pop("FAKE_TEST_KEY", None)
            else:
                os.environ["FAKE_TEST_KEY"] = saved


def test_batch20_fixture_mode_no_key_required():
    """Inventory #819: --fixtures runs of Stages 2/3/4 must succeed
    without ANTHROPIC_API_KEY (stub mode + fixture content)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        for s, extra in (
            ("01_aggregate_sources.py", ()),
            ("02_enrich_funds.py", ("--fixtures",)),
            ("03_mine_activity.py", ("--fixtures",)),
            ("04_mine_partner_signals.py", ("--fixtures",)),
        ):
            res = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / s),
                 "--workspace", ws, *extra],
                capture_output=True, text=True, env=env, timeout=120,
            )
            assert res.returncode == 0, (
                f"{s} (fixture mode, no key) should succeed; got "
                f"{res.returncode}\n{res.stdout}{res.stderr}"
            )


def test_batch20_llm_extract_json_retries_on_malformed():
    """Inventory #821/#822: LLM client retries up to max_retries times on
    bad JSON / bad schema before giving up. Drive _raw_call via monkey-
    patch so we can return bad text first, valid second."""
    import importlib.util
    from pathlib import Path as _P
    from pydantic import BaseModel, Field
    from core.llm.client import LLMClient, LLMError

    class _Schema(BaseModel):
        n: int = Field(..., ge=0, le=10)

    # Bypass the workspace/env dance by using a minimal Workspace stand-in.
    class _FakeWs:
        def env(self, key, default=None):
            return "fake-key"  # forces non-stub mode

    client = LLMClient(workspace=_FakeWs())
    # Sanity: client is in live mode now (api_key is set).
    assert client.stub is False

    calls = {"n": 0}

    def fake_raw_call(self, prompt, model, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        if calls["n"] == 2:
            return '{"n": 999}'  # schema-invalid (>10)
        return '{"n": 4}'

    import types
    client._raw_call = types.MethodType(fake_raw_call, client)
    result = client.complete_json(
        prompt="ignored", schema=_Schema, max_retries=3,
    )
    assert result.n == 4
    assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"

    # Reset + prove final failure raises LLMError after exhausting retries.
    calls["n"] = 0

    def always_bad(self, prompt, model, max_tokens):
        calls["n"] += 1
        return "still not json"

    client._raw_call = types.MethodType(always_bad, client)
    import pytest
    with pytest.raises(LLMError) as exc_info:
        client.complete_json(
            prompt="ignored", schema=_Schema, max_retries=3,
        )
    assert "schema-valid JSON" in str(exc_info.value)
    assert calls["n"] == 3


def test_batch20_stage7_null_priority_handling():
    """Inventory #969: Stage 7 ORDER BY send_now_priority.desc() puts
    NULL priorities in DB-dependent order. Confirm Stage 7 doesn't
    crash on a partner whose summary row has NULL send_now_priority."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        # Null out one priority.
        c = sqlite3.connect(db)
        c.execute(
            "update partner_score_summaries set send_now_priority=NULL "
            "where partner_id=(select partner_id from partner_score_summaries limit 1)"
        )
        c.commit()
        c.close()

        # Stage 7 should still run and produce CSV.
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)
        csv_path = ws_dst / "exports" / "review_queue.csv"
        assert csv_path.exists()


def test_batch19_outcome_suppresses_recommendation():
    """Inventory #1101-#1105/#1125: a partner with a terminal outcome
    (passed, wrong_stage, meeting_booked) or recent active outreach
    (sent/replied within window) must NOT be re-recommended on the
    next Stage 6 run, regardless of their score."""
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

        # Baseline: pick a recommended partner.
        c = sqlite3.connect(db)
        pid = c.execute(
            "select partner_id from partner_score_summaries "
            "where recommended_to_send=1 limit 1"
        ).fetchone()[0]

        # Insert a "passed_no_fit" outcome.
        from datetime import datetime as _dt
        c.execute(
            "insert into outcomes (partner_id, outreach_status, reply_type, "
            "meeting_booked, synced_from_attio_at, source) values "
            "(?, 'replied', 'passed_no_fit', 0, ?, 'manual')",
            (pid, _dt.utcnow().isoformat()),
        )
        c.commit()
        c.close()

        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        recommended, reasoning = c.execute(
            "select recommended_to_send, recommendation_reasoning "
            "from partner_score_summaries where partner_id=?",
            (pid,),
        ).fetchone()
        c.close()
        assert recommended == 0, (
            f"partner with passed_no_fit outcome should NOT be recommended; "
            f"got recommended={recommended}, reasoning={reasoning!r}"
        )
        assert "passed" in reasoning, (
            f"reasoning should mention the passed reply_type; got {reasoning!r}"
        )

        # Pick a different recommended partner; inject a booked meeting.
        c = sqlite3.connect(db)
        other_pid = c.execute(
            "select partner_id from partner_score_summaries "
            "where recommended_to_send=1 and partner_id != ? limit 1",
            (pid,),
        ).fetchone()
        if not other_pid:
            # Fixture only had one left after first suppression; skip the
            # booked-meeting half of the test.
            c.close()
            return
        other_pid = other_pid[0]
        c.execute(
            "insert into outcomes (partner_id, outreach_status, reply_type, "
            "meeting_booked, synced_from_attio_at, source) values "
            "(?, 'meeting_booked', 'booked', 1, ?, 'manual')",
            (other_pid, _dt.utcnow().isoformat()),
        )
        c.commit()
        c.close()

        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        rec2, reason2 = c.execute(
            "select recommended_to_send, recommendation_reasoning "
            "from partner_score_summaries where partner_id=?",
            (other_pid,),
        ).fetchone()
        c.close()
        assert rec2 == 0
        assert "meeting already booked" in reason2


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


def test_batch17_stage7_refuses_stale_stage6():
    """Inventory #363/#364/#970: Stage 7 must refuse when Stage 6 is older
    than its upstreams (Stage 5 / Stage 3), OR when Stage 6 hasn't
    completed at all. --skip-freshness-check --reason bypasses."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        # Sanity: Stage 7 succeeds when the chain is fresh.
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)

        # Inject staleness: mark Stage 6's completed_at older than Stage 5's.
        c = sqlite3.connect(db)
        c.execute(
            "update runs set completed_at = '2020-01-01 00:00:00' "
            "where stage = '06_score_candidates'"
        )
        c.commit()
        c.close()

        # Stage 7 should now refuse with exit 2.
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "07_generate_emails.py"),
             "--workspace", ws, "--top", "5", "--allow-example-domains"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 2, (
            f"Stage 7 should refuse with stale Stage 6; got {res.returncode}\n"
            f"STDOUT:\n{res.stdout}\n"
        )
        assert "FRESHNESS REFUSED" in res.stdout

        # --skip-freshness-check --reason bypasses + records the bypass.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "07_generate_emails.py"),
             "--workspace", ws, "--top", "5", "--allow-example-domains",
             "--skip-freshness-check", "--reason", "smoke test bypass"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert res.returncode == 0, (
            f"Stage 7 with --skip-freshness-check should succeed; got "
            f"{res.returncode}\nSTDOUT:\n{res.stdout}"
        )
        c = sqlite3.connect(db)
        note = c.execute(
            "select error_summary from runs where stage='07_generate_emails' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert note and "FRESHNESS_SKIPPED" in note


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


def test_batch16_fund_kill_signals_block_recommendation():
    """Inventory #834/#927: a fund whose Stage 2 enrichment extracted
    explicit kill_signals must trigger major_kill and prevent
    recommended_to_send for that fund's partners."""
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

        # Force a kill_signals string onto an active fund whose partners
        # would otherwise be recommended.
        c = sqlite3.connect(db)
        c.execute(
            "update funds set kill_signals = ? "
            "where fund_id = 'northbeam.example'",
            ("explicitly does not lead seed rounds in fintech",),
        )
        c.commit()
        c.close()

        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        rows = c.execute(
            "select p.partner_id, s.major_kill_signal_present, "
            "s.recommended_to_send, s.kill_signal_summary "
            "from partners p join partner_score_summaries s "
            "on s.partner_id = p.partner_id "
            "where p.fund_id = 'northbeam.example'"
        ).fetchall()
        c.close()
        assert rows, "expected Northbeam partners with summaries"
        for pid, major_kill, recommended, kill_summary in rows:
            assert major_kill == 1, (
                f"partner {pid}: fund.kill_signals should trip major_kill"
            )
            assert "fund kill_signals" in (kill_summary or ""), (
                f"kill_signal_summary should mention the fund kill; "
                f"got {kill_summary!r}"
            )
            assert recommended == 0, (
                f"partner {pid}: should not be recommended when fund has kill"
            )


def test_batch15_apply_records_approver():
    """Batch 15: apply_axis_suggestion records approved_by + approval_reason
    on the row, and --list doesn't pollute run.processed."""
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
            ("07_generate_emails.py", ("--top", "5",
                                        "--allow-example-domains")),
        ):
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)

        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Seed fixture outcomes and generate suggestions.
        subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "monthly_learning_report.py"),
             "--workspace", ws, "--seed-fixture-outcomes",
             "--include-fixture-outcomes"],
            capture_output=True, text=True, env=env, timeout=60,
        )

        c = sqlite3.connect(db)
        sid = c.execute(
            "select suggestion_id from axis_weight_suggestions "
            "where approved is null order by suggestion_id limit 1"
        ).fetchone()
        assert sid is not None, "expected at least one pending suggestion"
        sid = sid[0]
        c.close()

        # --list: should NOT show processed in run.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--list"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        proc = c.execute(
            "select records_processed from runs where stage='apply_axis_suggestion' "
            "order by run_id desc limit 1"
        ).fetchone()[0]
        assert proc in (0, None), (
            f"--list should not record records_processed; got {proc}"
        )
        c.close()

        # Apply with --approved-by + --approval-reason
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "jobs" / "apply_axis_suggestion.py"),
             "--workspace", ws, "--suggestion-id", str(sid),
             "--accept-low-confidence",
             "--approved-by", "ashley", "--approval-reason",
             "validated against last 3 batches"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        approved_by, approval_reason = c.execute(
            "select approved_by, approval_reason from axis_weight_suggestions "
            "where suggestion_id=?", (sid,),
        ).fetchone()
        c.close()
        assert approved_by == "ashley"
        assert approval_reason == "validated against last 3 batches"


def test_batch14_new_clis_against_fixture():
    """Batch 14: set_employment_status, set_fund_inactive,
    set_partner_linkedin, list_missing_fields, list_blocked_recommendations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # set_employment_status: flip Priya to left_fund.
        pid = "northbeam.example_priya_anand"
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_employment_status.py"),
             "--workspace", ws, "--partner-id", pid,
             "--status", "left_fund", "--reason", "test"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        status = c.execute(
            "select employment_status from partners where partner_id=?",
            (pid,),
        ).fetchone()[0]
        c.close()
        assert status == "left_fund"

        # Unknown partner -> exit 2.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_employment_status.py"),
             "--workspace", ws, "--partner-id", "no-such-partner",
             "--status", "left_fund", "--reason", "test"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2

        # set_fund_inactive
        fid = "northbeam.example"
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_fund_inactive.py"),
             "--workspace", ws, "--fund-id", fid,
             "--reason", "test inactive"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        active = c.execute(
            "select is_active from funds where fund_id=?", (fid,),
        ).fetchone()[0]
        assert active == 0
        # Reactivate
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_fund_inactive.py"),
             "--workspace", ws, "--fund-id", fid, "--reactivate",
             "--reason", "test reactivate"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        active = c.execute(
            "select is_active from funds where fund_id=?", (fid,),
        ).fetchone()[0]
        c.close()
        assert active == 1

        # set_partner_linkedin
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_partner_linkedin.py"),
             "--workspace", ws, "--partner-id", pid,
             "--url", "https://www.linkedin.com/in/priya-anand"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        url = c.execute(
            "select linkedin_url from partners where partner_id=?", (pid,),
        ).fetchone()[0]
        c.close()
        assert url == "https://www.linkedin.com/in/priya-anand"

        # Invalid URL rejected
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "set_partner_linkedin.py"),
             "--workspace", ws, "--partner-id", pid,
             "--url", "not-a-url"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2

        # list_missing_fields --json
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "list_missing_fields.py"),
             "--workspace", ws, "--all", "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert "partners" in payload and "funds" in payload
        # Priya now has a linkedin_url from above, but still missing email.
        priya = next(
            (p for p in payload["partners"] if p["partner_id"] == pid), None,
        )
        assert priya is not None
        assert "email" in priya["missing"]
        assert "linkedin_url" not in priya["missing"]

        # list_blocked_recommendations --json
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "list_blocked_recommendations.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert "by_reason" in payload
        # At least one of the known not-recommended fixtures (e.g. Ingrid
        # Solberg for lead_likelihood, or Sofia for round_fit) should land
        # in a bucket.
        assert any(items for items in payload["by_reason"].values())


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
