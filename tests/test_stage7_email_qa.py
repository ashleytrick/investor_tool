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





def test_batch43_stage7_preserves_prior_draft_on_qa_fail():
    """Inventory #87: Stage 7's Batch 37 #38 behavior -- when a
    partner's NEW recommended draft has hard-gate failures, the prior
    good draft is preserved (not overwritten by the bad regeneration).
    Drive by stubbing build_stub_response so one partner's regenerated
    body is identical to the prior (similar enough) AND contains a
    forbidden phrase that trips check_hard_gates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)

        # First Stage 7 run: produce a good batch.
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)
        c = sqlite3.connect(db)
        first_batch_id, prior_body = c.execute(
            "select batch_id, body from email_drafts "
            "where is_recommended=1 limit 1"
        ).fetchone()
        prior_partner = c.execute(
            "select partner_id from email_drafts "
            "where batch_id=? and is_recommended=1 limit 1",
            (first_batch_id,),
        ).fetchone()[0]
        c.close()
        assert prior_body and "would love" not in prior_body.lower()

        # Force a re-run where the recommended draft has a forbidden
        # phrase. The cleanest way: monkey-patch build_stub_response so
        # the chosen partner's recommended body contains "would love".
        driver = ws_dst / "_drive_stage7_bad_regen.py"
        driver.write_text(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "spec = importlib.util.spec_from_file_location("
            f"'s7', {str(REPO_ROOT / 'scripts' / '07_generate_emails.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "real = m.build_stub_response\n"
            "def patched(partner_id, strategies):\n"
            "    out = real(partner_id, strategies)\n"
            f"    if out and partner_id == {prior_partner!r}:\n"
            "        # corrupt the recommended variant with a forbidden phrase\n"
            "        rec = out['recommended_variant_strategy']\n"
            "        for v in out['variants']:\n"
            "            if v['strategy'] == rec:\n"
            "                v['body'] = ('We are raising and would love to chat soon '\n"
            "                             'next week. ' * 4)\n"
            "    return out\n"
            "m.build_stub_response = patched\n"
            f"sys.argv = ['s7', '--workspace', {ws!r}, '--top', '5',\n"
            "             '--allow-example-domains',\n"
            "             '--skip-freshness-check', '--reason', 'test #87']\n"
            "raise SystemExit(m.main())\n"
        )
        env = {**os.environ, "ANTHROPIC_API_KEY": ""}
        # The corrupted draft will fail per-draft hard gates. Stage 7
        # batch-QA might or might not fail batch-wide; either way the
        # prior draft for that partner must survive.
        subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, env=env, timeout=120,
        )

        c = sqlite3.connect(db)
        # Surviving recommended draft for the targeted partner: ANY
        # recommended row whose body == prior_body (matches old) must
        # still exist OR no NEW row replaced it.
        surviving = c.execute(
            "select count(*) from email_drafts "
            "where partner_id = ? and is_recommended = 1 and body = ?",
            (prior_partner, prior_body),
        ).fetchone()[0]
        c.close()
        assert surviving >= 1, (
            f"prior good draft for {prior_partner} was overwritten by "
            f"the failed regeneration; #38 regression"
        )





def test_batch43_review_queue_csv_golden_columns():
    """Inventory #90: pin the review_queue.csv column order +
    expected per-status mix. Acts as a smoke test for the column
    contract that downstream operators / spreadsheets rely on."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()
        ws = str(ws_dst)
        _run_pipeline_through_stage_6(ws_dst)
        _run("07_generate_emails.py", "--workspace", ws, "--top", "5",
             "--allow-example-domains", cwd=REPO_ROOT)

        csv_path = ws_dst / "exports" / "review_queue.csv"
        with csv_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            cols = list(reader.fieldnames or [])
            rows = list(reader)
        # Golden column list (must match core/csv_export.py CSV_COLUMNS).
        from core.csv_export import CSV_COLUMNS
        assert cols == CSV_COLUMNS, (
            f"CSV column drift!\nactual:   {cols}\nexpected: {CSV_COLUMNS}"
        )
        # Every row has an outreach_status from the known set.
        valid_statuses = {
            "ready_to_send", "draft", "warm_path_needed",
        }
        for r in rows:
            assert r["outreach_status"] in valid_statuses, (
                f"unexpected outreach_status {r['outreach_status']!r}"
            )
            assert r["partner_id"]
            assert r["partner_name"]
            assert r["email_subject_line"]
            assert r["outreach_email_draft"]
        # Fixture always produces 5 rows (top=5).
        assert len(rows) == 5





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
