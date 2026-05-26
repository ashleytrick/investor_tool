"""Tests for the Slice 14 setup wizard."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT


def _run_wizard(*extra: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "init_wizard.py"), *extra],
        capture_output=True, text=True, cwd=cwd or REPO_ROOT, timeout=30,
    )


@pytest.fixture
def isolated_clients(monkeypatch, tmp_path: Path):
    """Override REPO_ROOT inside init_wizard so the test writes into
    tmp_path/clients/ instead of the repo's clients/ directory.
    Cleans up automatically."""
    fake_repo = tmp_path
    (fake_repo / "clients").mkdir()
    # Patch REPO_ROOT in both init_wizard and init_workspace before
    # the script is imported / invoked.
    monkeypatch.setenv("INVESTOR_WIZARD_REPO_ROOT_OVERRIDE", str(fake_repo))
    return fake_repo


def test_non_interactive_writes_company_yaml_with_answers(tmp_path):
    """Drive the wizard with every required flag and assert the
    generated company.yaml carries the substituted values."""
    # Write to a tmp-dir relative path so the test doesn't pollute
    # the real clients/ directory. The CLI hardcodes REPO_ROOT /
    # clients; cleanest is to invoke with a slug that we delete
    # afterwards.
    slug = f"wizard_test_{os.getpid()}"
    target = REPO_ROOT / "clients" / slug
    try:
        res = _run_wizard(
            slug, "--non-interactive",
            "--company-name", "Oko",
            "--founder-name", "Ashley Trick",
            "--founder-email", "ashley@oko.com",
            "--one-liner", "We index VC fund activity for founders",
            "--scheduling-link", "https://cal.com/ashley/oko-vc",
            "--mode", "dry_run",
            "--target-sector", "fintech",
            "--target-sector", "infra",
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert target.exists()
        company = (target / "config" / "company.yaml").read_text()
        assert 'name: "Oko"' in company
        assert 'founder_name: "Ashley Trick"' in company
        assert 'founder_email: "ashley@oko.com"' in company
        assert 'preferred_scheduling_link: "https://cal.com/ashley/oko-vc"' in company
        assert "mode: dry_run" in company
        assert '- "fintech"' in company
        assert '- "infra"' in company
        # Other sections still carry the {PLACEHOLDER} strings the
        # operator will edit later -- the wizard is intentionally
        # focused on the must-know-up-front fields.
        assert "{TARGET_RAISE}" in company
        # Sibling configs landed.
        for fname in ("axes.yaml", "sources.yaml", "attio.yaml"):
            assert (target / "config" / fname).exists()
        # Per-workspace .gitignore exists.
        assert (target / ".gitignore").exists()
    finally:
        if target.exists():
            shutil.rmtree(target)


def test_non_interactive_refuses_on_missing_required_field():
    """No --founder-email -> the wizard refuses with a clear message."""
    slug = f"wizard_test_missing_{os.getpid()}"
    res = _run_wizard(
        slug, "--non-interactive",
        "--company-name", "Oko",
        "--founder-name", "Ashley",
        # missing --founder-email
        "--one-liner", "We do things",
        "--scheduling-link", "https://cal.com/x",
        "--target-sector", "fintech",
    )
    assert res.returncode == 2
    assert "founder-email" in res.stdout


def test_non_interactive_validates_email_format():
    slug = f"wizard_test_bademail_{os.getpid()}"
    res = _run_wizard(
        slug, "--non-interactive",
        "--company-name", "Oko",
        "--founder-name", "Ashley",
        "--founder-email", "not-an-email",
        "--one-liner", "We do things",
        "--scheduling-link", "https://cal.com/x",
        "--target-sector", "fintech",
    )
    assert res.returncode == 2
    assert "email" in res.stdout.lower()


def test_non_interactive_validates_scheduling_link_scheme():
    slug = f"wizard_test_badurl_{os.getpid()}"
    res = _run_wizard(
        slug, "--non-interactive",
        "--company-name", "Oko",
        "--founder-name", "Ashley",
        "--founder-email", "ashley@oko.com",
        "--one-liner", "We do things",
        "--scheduling-link", "cal.com/x",  # missing http(s)://
        "--target-sector", "fintech",
    )
    assert res.returncode == 2
    assert "http" in res.stdout.lower()


def test_non_interactive_refuses_existing_workspace_without_force():
    """Running the wizard twice on the same slug without --force
    refuses -- protects the operator from accidentally clobbering
    their config."""
    slug = f"wizard_test_existing_{os.getpid()}"
    target = REPO_ROOT / "clients" / slug
    common = [
        slug, "--non-interactive",
        "--company-name", "Oko",
        "--founder-name", "Ashley",
        "--founder-email", "ashley@oko.com",
        "--one-liner", "We do things",
        "--scheduling-link", "https://cal.com/x",
        "--target-sector", "fintech",
    ]
    try:
        # First run succeeds.
        res = _run_wizard(*common)
        assert res.returncode == 0
        # Second run refuses.
        res2 = _run_wizard(*common)
        assert res2.returncode == 2
        assert "already exists" in res2.stdout
        # With --force the second run succeeds.
        res3 = _run_wizard(*common, "--force")
        assert res3.returncode == 0
    finally:
        if target.exists():
            shutil.rmtree(target)


def test_check_ready_runs_against_wizard_output(tmp_path):
    """Sanity check: a workspace scaffolded by the wizard boots cleanly
    enough for check_ready.py to run without crashing. (It'll BLOCK
    on multiple checks since Stage 7 hasn't run, but it shouldn't
    crash on missing config keys -- the wizard's job is to seed those.)
    """
    slug = f"wizard_test_checkready_{os.getpid()}"
    target = REPO_ROOT / "clients" / slug
    try:
        res = _run_wizard(
            slug, "--non-interactive",
            "--company-name", "Oko",
            "--founder-name", "Ashley",
            "--founder-email", "ashley@oko.com",
            "--one-liner", "We do things",
            "--scheduling-link", "https://example.test/cal",
            "--mode", "dry_run",
            "--target-sector", "fintech",
        )
        assert res.returncode == 0, res.stdout + res.stderr
        # check_ready doesn't need the pipeline to have run.
        cr = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_ready.py"),
             "--workspace", str(target)],
            capture_output=True, text=True, timeout=30,
        )
        # check_ready exits non-zero because Stage 6 hasn't run, the
        # approval pipeline is empty, etc. -- that's fine. The wizard
        # contract is: it boots, the banner prints, every check returns
        # SOMETHING (no crash).
        assert "[check_ready]" in cr.stdout
        # The wizard set mode=dry_run; check_ready's mode check is OK
        # (only fixture mode blocks).
        assert "mode: OK" in cr.stdout
    finally:
        if target.exists():
            shutil.rmtree(target)
