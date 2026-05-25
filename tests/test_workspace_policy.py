"""Unit tests for core/workspace_policy.py (Refactor item 10 / Slice 13)."""
from __future__ import annotations

import argparse
from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.workspace_policy import WorkspacePolicy


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _ws(mode: str | None) -> SimpleNamespace:
    return SimpleNamespace(mode=mode)


def test_production_mode_makes_require_flags_implicit() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws("production"), _args())
    assert p.require_attio is True
    assert p.require_gmail is True
    assert p.require_anthropic is True
    assert p.require_ready_to_send is True
    # Permission gates stay strict in production.
    assert p.allow_example_domains is False
    assert p.allow_fixture_data is False
    # Production is the ONLY mode that allows external mutations.
    assert p.refuses_external_mutation() is False


def test_fixture_mode_is_permissive_but_refuses_real_writes() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws("fixture"), _args())
    assert p.require_attio is False
    assert p.require_gmail is False
    assert p.refuses_fixture_data() is True
    # --allow-fixture-mode opts in.
    p2 = WorkspacePolicy.from_workspace_and_args(
        _ws("fixture"), _args(allow_fixture_mode=True),
    )
    assert p2.refuses_fixture_data() is False
    # Fixture mode never calls external mutation APIs, even with
    # --allow-fixture-mode (the flag is for refusing fixture-DATA
    # syncs, NOT for granting external-write permission).
    assert p.refuses_external_mutation() is True
    assert p2.refuses_external_mutation() is True


def test_dry_run_mode_skips_missing_integrations() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws("dry_run"), _args())
    assert p.require_attio is False
    assert p.integration_skip_or_fail(system="attio") == "skip"
    assert p.integration_skip_or_fail(system="gmail") == "skip"
    # dry_run NEVER calls external mutation APIs even when creds set.
    assert p.refuses_external_mutation() is True


def test_explicit_require_flag_overrides_dry_run_default() -> None:
    p = WorkspacePolicy.from_workspace_and_args(
        _ws("dry_run"), _args(require_attio=True),
    )
    assert p.require_attio is True
    assert p.integration_skip_or_fail(system="attio") == "fail"
    # Untouched integrations still skip.
    assert p.integration_skip_or_fail(system="gmail") == "skip"
    # require_attio is about HAVING the config; it doesn't unlock
    # mutation in dry_run mode.
    assert p.refuses_external_mutation() is True


def test_include_not_ready_opts_out_of_production_require_ready() -> None:
    p = WorkspacePolicy.from_workspace_and_args(
        _ws("production"), _args(include_not_ready=True),
    )
    assert p.require_ready_to_send is False
    # The other production defaults still hold.
    assert p.require_attio is True


def test_missing_mode_defaults_to_dry_run() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws(None), _args())
    assert p.mode == "dry_run"
    assert p.require_attio is False
    assert p.refuses_external_mutation() is True


def test_missing_args_attribute_defaults_to_false() -> None:
    """Stages only declare the flags they accept; the policy fills in
    False for anything missing on the Namespace."""
    args = _args()  # zero flags
    p = WorkspacePolicy.from_workspace_and_args(_ws("dry_run"), args)
    assert p.allow_example_domains is False
    assert p.allow_fixture_data is False
    assert p.allow_unknown_partner_ids is False


# ----- Slice 13 legacy-name aliases -----


def test_legacy_prod_alias_resolves_to_production() -> None:
    """Existing workspaces with `mode: prod` keep working; the policy
    normalizes to the canonical 'production'."""
    p = WorkspacePolicy.from_workspace_and_args(_ws("prod"), _args())
    assert p.mode == "production"
    assert p.require_attio is True
    assert p.refuses_external_mutation() is False


def test_legacy_dev_alias_resolves_to_dry_run() -> None:
    """`mode: dev` is the legacy name for dry_run."""
    p = WorkspacePolicy.from_workspace_and_args(_ws("dev"), _args())
    assert p.mode == "dry_run"
    assert p.require_attio is False
    assert p.refuses_external_mutation() is True


# ----- Slice 13 dry_run external-mutation refusal (integration) -----


def test_dry_run_mode_blocks_stage_8_attio_sync(tmp_path):
    """A workspace in mode=dry_run should NOT call the Attio API even
    if attio.yaml + ATTIO_API_KEY are configured. The script logs the
    skip and exits 0."""
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys

    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)

    # Flip the fixture workspace into dry_run mode + provide a fake
    # attio.yaml so the script reaches the refusal check rather than
    # short-circuiting on the missing-config path.
    cfg = (ws_dst / "config" / "company.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("mode: fixture", "mode: dry_run")
    (ws_dst / "config" / "company.yaml").write_text(cfg, encoding="utf-8")
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
    env = {**os.environ, "ATTIO_API_KEY": "would-be-real-if-mode-prod"}

    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "08_sync_to_attio.py"),
         "--workspace", str(ws_dst)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "mode=dry_run" in res.stdout
    assert "Attio writes refused" in res.stdout

    # The run row should record the skip.
    db = ws_dst / "data" / "pipeline.db"
    c = sqlite3.connect(db)
    skipped, summary = c.execute(
        "select records_skipped, error_summary from runs "
        "where stage='08_sync_to_attio' order by run_id desc limit 1"
    ).fetchone()
    c.close()
    assert skipped >= 1
    assert "dry_run" in (summary or "")
