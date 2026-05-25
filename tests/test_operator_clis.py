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
        # connect_gmail (mode=fixture override needed for the test workspace).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "create_gmail_drafts.py"),
             "--workspace", ws, "--allow-fixture-mode"],
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





def test_batch35_set_partner_email_validation_and_exit():
    """Inventory followup: set_partner_email rejects bad email shape,
    exits 2 when ANY row failed (unknown partner OR bad email)."""
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
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Bad email shape -> exit 2.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
             "--workspace", ws, "--partner-id",
             "northbeam.example_priya_anand", "--email", "not-an-email"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2, (
            f"bad email should exit 2; got {res.returncode}\n{res.stdout}"
        )
        assert "REFUSED" in res.stdout or "not a valid" in res.stdout

        # Unknown partner -> exit 2.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
             "--workspace", ws, "--partner-id", "no-such-partner",
             "--email", "x@y.com"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2

        # Valid email + known partner -> exit 0.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_partner_email.py"),
             "--workspace", ws, "--partner-id",
             "northbeam.example_priya_anand", "--email", "priya@northbeam.example"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0





def test_batch35_gmail_not_configured_records_skipped_run():
    """Gmail not configured used to return 0 with NO run row.
    Batch 35: opens RunLogger with skipped=1 so status.py + audit
    can see when this stage was last attempted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run("01_aggregate_sources.py", "--workspace", ws, cwd=REPO_ROOT)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Run create_gmail_drafts with fixture mode allowed (no creds).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "create_gmail_drafts.py"),
             "--workspace", ws, "--allow-fixture-mode"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0  # skip is success
        assert "Gmail not linked" in res.stdout

        c = sqlite3.connect(db)
        row = c.execute(
            "select records_skipped, error_summary from runs "
            "where stage='create_gmail_drafts' order by run_id desc limit 1"
        ).fetchone()
        c.close()
        assert row is not None, (
            "Gmail-not-configured run should land in `runs` table for audit"
        )
        skipped, summary = row
        assert skipped == 1
        assert summary and "Gmail not linked" in summary





def test_batch34_attribution_overrides_and_backfill():
    """Inventory #346/#757/#758/#759/#760: operator overrides preserved
    across Stage 3 re-runs (reject, set), and backfill resolves
    previously-unmatched raw names against the updated DB."""
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
        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}

        # Pick an attribution with a real lead_fund_id to override.
        c = sqlite3.connect(db)
        row = c.execute(
            "select source_url, lead_fund_id from deal_attributions "
            "where lead_fund_id is not null limit 1"
        ).fetchone()
        assert row is not None
        target_url, original_fund = row
        c.close()

        # #758: --action set forces a different fund_id (use a real one).
        c = sqlite3.connect(db)
        other_fund = c.execute(
            "select fund_id from funds where fund_id != ? limit 1",
            (original_fund,),
        ).fetchone()[0]
        c.close()
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "correct_deal_attribution.py"),
             "--workspace", ws, "--source-url", target_url,
             "--action", "set", "--fund-id", other_fund,
             "--reason", "operator override"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        applied = c.execute(
            "select lead_fund_id from deal_attributions where source_url = ?",
            (target_url,),
        ).fetchone()[0]
        c.close()
        assert applied == other_fund, (
            f"override should have updated fund; got {applied}"
        )

        # #760: re-run Stage 3 -- the override survives.
        _run("03_mine_activity.py", "--workspace", ws, "--fixtures",
             cwd=REPO_ROOT)
        c = sqlite3.connect(db)
        after_rerun = c.execute(
            "select lead_fund_id from deal_attributions where source_url = ?",
            (target_url,),
        ).fetchone()[0]
        c.close()
        assert after_rerun == other_fund, (
            f"override should survive Stage 3 re-run; got {after_rerun}"
        )

        # #757: --action reject wipes attribution but leaves skeleton.
        # Pick a DIFFERENT source_url.
        c = sqlite3.connect(db)
        reject_url = c.execute(
            "select source_url from deal_attributions where source_url != ? "
            "and lead_fund_id is not null limit 1",
            (target_url,),
        ).fetchone()
        c.close()
        assert reject_url is not None
        reject_url = reject_url[0]
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "correct_deal_attribution.py"),
             "--workspace", ws, "--source-url", reject_url,
             "--action", "reject", "--reason", "not actually a funding event"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        c = sqlite3.connect(db)
        rej_after = c.execute(
            "select lead_fund_id, attributed_partner_id, raw_lead_investor "
            "from deal_attributions where source_url = ?",
            (reject_url,),
        ).fetchone()
        c.close()
        assert rej_after[0] is None
        assert rej_after[1] is None
        # raw_lead_investor preserved (skeleton).
        assert rej_after[2] is not None

        # #759: refuse to set with a non-existent fund/partner id.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "correct_deal_attribution.py"),
             "--workspace", ws, "--source-url", target_url,
             "--action", "set", "--fund-id", "nonexistent.example",
             "--reason", "should fail"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 2

        # --list shows the active overrides.
        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "correct_deal_attribution.py"),
             "--workspace", ws, "--list"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        assert target_url in res.stdout
        assert reject_url in res.stdout

        # #346 backfill: inject an unresolved partner-attribution row whose
        # raw name resolves to an existing partner (simulating Stage 2
        # having JUST discovered them).
        c = sqlite3.connect(db)
        # Pick a known partner + their fund.
        pid, fname = c.execute(
            "select p.partner_id, f.name from partners p "
            "join funds f on f.fund_id = p.fund_id limit 1"
        ).fetchone()
        # Get the partner's display name to feed into raw_attributed_partners.
        partner_name = c.execute(
            "select name from partners where partner_id = ?", (pid,)
        ).fetchone()[0]
        c.execute(
            "insert into deal_attributions (source_url, raw_lead_investor, "
            "raw_attributed_partners, captured_at) values "
            "(?, NULL, ?, datetime('now'))",
            (
                "https://example.invalid/test-backfill",
                json.dumps([{"name": partner_name, "fund": fname}]),
            ),
        )
        c.commit()
        c.close()

        res = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "backfill_attributions.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        c = sqlite3.connect(db)
        backfilled = c.execute(
            "select attributed_partner_id, lead_fund_id from deal_attributions "
            "where source_url = 'https://example.invalid/test-backfill'"
        ).fetchone()
        c.close()
        assert backfilled[0] == pid, (
            f"backfill should resolve partner_id; got {backfilled[0]}"
        )





def test_batch31_compare_and_restore_batches():
    """Inventory #698/#699: compare_batches diffs two Stage 7 batches and
    restore_batch rebuilds review_queue.csv from a prior batch."""
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

        # Run Stage 7 once, then synthesize a second batch by copying the
        # current recommended drafts under a new batch_id. (Re-running
        # Stage 7 wipes prior drafts for the same partners; the
        # compare/restore tools are designed for cases where you've kept
        # an older batch around in the DB.)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)
        c = sqlite3.connect(db)
        first_batch = c.execute(
            "select distinct batch_id from email_drafts order by batch_id"
        ).fetchone()[0]
        c.execute(
            "insert into email_drafts (partner_id, batch_id, strategy, "
            "subject, body, conversion_hypothesis, likely_objection, "
            "objection_preempted, preemption_line, template_smell, "
            "qa_status, is_recommended, generated_at) "
            "select partner_id, 'batch_synthetic_older', strategy, "
            "subject, body, conversion_hypothesis, likely_objection, "
            "objection_preempted, preemption_line, template_smell, "
            "qa_status, is_recommended, generated_at "
            "from email_drafts where batch_id = ?",
            (first_batch,),
        )
        c.commit()
        c.close()

        # compare_batches --json should return two batch ids + zero added
        # / zero dropped (synthetic second batch has the same partners).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "compare_batches.py"),
             "--workspace", ws, "--json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        diff = json.loads(res.stdout)
        assert diff["before"] != diff["after"]
        assert diff["added"] == []
        assert diff["dropped"] == []

        # restore_batch --list shows both batches.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "restore_batch.py"),
             "--workspace", ws, "--list"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        # Two batch_ids listed.
        assert res.stdout.count("batch_") >= 2

        # restore_batch from the synthetic older batch -- CSV gets rewritten
        # with the "RESTORED from" tag in recommendation_reasoning.
        csv_path = ws_dst / "exports" / "review_queue.csv"
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "restore_batch.py"),
             "--workspace", ws, "--batch-id", "batch_synthetic_older"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0, res.stderr
        after_text = csv_path.read_text()
        assert "RESTORED from" in after_text





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
             "--reason", "conflict of interest",
             "--set-by", "alice"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        _run("06_score_candidates.py", "--workspace", ws, cwd=REPO_ROOT)

        c = sqlite3.connect(db)
        rec, summary = c.execute(
            "select recommended_to_send, kill_signal_summary "
            "from partner_score_summaries where partner_id=?", (pid,),
        ).fetchone()
        # Slice 15: DNC metadata columns populated by the CLI.
        meta = c.execute(
            "select do_not_contact_set_at, do_not_contact_set_by, "
            "do_not_contact_source from partners where partner_id=?",
            (pid,),
        ).fetchone()
        c.close()
        assert rec == 0, "do_not_contact partner must not be recommended"
        assert "do_not_contact" in (summary or ""), (
            f"kill_signal_summary should mention do_not_contact; "
            f"got {summary!r}"
        )
        # set_at is an ISO timestamp string from SQLite; just confirm
        # it was populated (not NULL) along with operator + source.
        assert meta[0] is not None, "do_not_contact_set_at must be set"
        assert meta[1] == "alice"
        assert meta[2] == "manual"

        # Clear the flag, re-run, expect recommendation restored (if it
        # was the only blocker -- the fixture partners are otherwise
        # qualified so this should work).
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "set_do_not_contact.py"),
             "--workspace", ws, "--partner-id", pid, "--clear"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert res.returncode == 0
        # Clearing wipes the audit metadata so a future audit can
        # distinguish "currently clear" from "currently set".
        c = sqlite3.connect(db)
        cleared = c.execute(
            "select do_not_contact_set_at, do_not_contact_set_by, "
            "do_not_contact_source from partners where partner_id=?",
            (pid,),
        ).fetchone()
        c.close()
        assert cleared == (None, None, None)
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
