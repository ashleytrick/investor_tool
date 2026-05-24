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





def test_batch36_stage6_lead_likelihood_none_blocks():
    """Inventory #22: parallel to the cold_reachability None fix --
    lead_likelihood_score=None now blocks recommendation."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s6", REPO_ROOT / "scripts" / "06_score_candidates.py"
    )
    s6 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s6)
    from datetime import date as _date

    base = dict(
        composite=7.5, round_fit_score=8.0, disqualifier_present=False,
        distinct_source_types=2, q2_plus_signal_count=2,
        deal_attribution_count=1,
        most_recent_signal_date=_date.today(),
        employment_status="likely_current", major_kill=False,
        warm_path_available=False, today=_date.today(),
        cold_reachability_score=7.0,
    )
    ok, _r = s6.evaluate_recommended(lead_likelihood_score=6.0, **base)
    assert ok is True
    ok, reason = s6.evaluate_recommended(lead_likelihood_score=None, **base)
    assert ok is False
    assert "lead_likelihood_score is unknown" in reason





def test_batch35_cold_reachability_unknown_blocks_recommendation():
    """Stage 6 used to permit recommendation when cold_reachability_score
    was None (the check was `is not None and < 5.0`). Now None blocks."""
    # evaluate_recommended() is the function under test; import via
    # importlib so we don't depend on the module's CLI setup.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s6", REPO_ROOT / "scripts" / "06_score_candidates.py"
    )
    s6 = importlib.util.module_from_spec(spec); spec.loader.exec_module(s6)
    from datetime import date as _date

    base = dict(
        composite=7.5, round_fit_score=8.0, disqualifier_present=False,
        lead_likelihood_score=6.0, distinct_source_types=2,
        q2_plus_signal_count=2, deal_attribution_count=1,
        most_recent_signal_date=_date.today(),
        employment_status="likely_current", major_kill=False,
        warm_path_available=False, today=_date.today(),
    )

    # cold_reachability=7.0 -> recommended.
    ok, _r = s6.evaluate_recommended(cold_reachability_score=7.0, **base)
    assert ok is True

    # cold_reachability=4.0 -> blocked (existing behavior).
    ok, reason = s6.evaluate_recommended(cold_reachability_score=4.0, **base)
    assert ok is False
    assert "cold_reachability_score" in reason and "< 5.0" in reason

    # cold_reachability=None -> blocked (Batch 35 fix).
    ok, reason = s6.evaluate_recommended(cold_reachability_score=None, **base)
    assert ok is False, (
        f"None reachability should block recommendation; got reason={reason!r}"
    )
    assert "unknown" in reason





def test_batch35_stage6_partial_failure_already_tested():
    """Stage 6's partial-failure exit code was added in Batch 11 and is
    pinned by test_stage6_returns_nonzero_when_partner_fails. This stub
    just asserts Stage 2/3/4 follow the same pattern by checking that
    a clean fixture run still exits 0 (regression: the new
    `return 2 if any_failed` shouldn't fire for a green pipeline)."""
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
            # _run() asserts exit 0, so this implicitly verifies the
            # new return-on-failure logic doesn't accidentally trip on
            # the green fixture path.
            _run(s, "--workspace", ws, *extra, cwd=REPO_ROOT)





def test_batch28_stage6_filter_mode_audit():
    """Inventory #358/#359/#360: Stage 6 with --partner-id surfaces the
    filter explicitly in stdout + run.note instead of misleading the
    operator with a workspace-wide recommended count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        pid = "northbeam.example_priya_anand"
        res = _run(
            "06_score_candidates.py", "--workspace", ws,
            "--partner-id", pid, cwd=REPO_ROOT,
        )
        assert "FILTER MODE" in res.stdout, (
            f"filter-mode summary missing; stdout=\n{res.stdout}"
        )
        c = sqlite3.connect(db)
        note = c.execute(
            "select error_summary from runs "
            "where stage='06_score_candidates' order by run_id desc limit 1"
        ).fetchone()[0]
        c.close()
        assert note and "filter mode" in note





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
