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





def test_batch30_fixture_mode_refusal():
    """Inventory #528/#529/#531: a workspace with company.yaml `mode:
    fixture` must refuse Stage 8 sync + Gmail draft creation without
    --allow-fixture-mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_src = REPO_ROOT / "clients" / "test_workspace"
        ws_dst = Path(tmpdir) / "test_workspace"
        shutil.copytree(ws_src, ws_dst)
        db = ws_dst / "data" / "pipeline.db"
        if db.exists():
            db.unlink()

        # The fixture workspace already has mode: fixture; confirm at the
        # Workspace API.
        from core.config_loader import Workspace
        ws_obj = Workspace(str(ws_dst))
        assert ws_obj.mode == "fixture"

        # Add a minimal attio.yaml so the Stage 8 preflight doesn't skip.
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

        _run_pipeline_through_stage_6(ws_dst)
        ws = str(ws_dst)
        env = {**os.environ, "ANTHROPIC_API_KEY": "", "ATTIO_API_KEY": "fake"}

        # Stage 8 without --allow-fixture-mode -> refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "08_sync_to_attio.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        assert "mode=fixture" in res.stdout

        # Gmail draft creation without --allow-fixture-mode -> refuse.
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "create_gmail_drafts.py"),
             "--workspace", ws],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert res.returncode == 2
        assert "mode=fixture" in res.stdout

        # Invalid mode is rejected at load time.
        bad_path = Path(tmpdir) / "bad_mode_workspace"
        shutil.copytree(ws_src, bad_path)
        bad_company = (bad_path / "config" / "company.yaml").read_text()
        (bad_path / "config" / "company.yaml").write_text(
            bad_company.replace("mode: fixture", "mode: staging"),
            encoding="utf-8",
        )
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            Workspace(str(bad_path))
        assert "must be one of" in str(exc_info.value)





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
