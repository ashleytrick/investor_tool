"""Unit tests for core/workspace_policy.py (Refactor item 10)."""
from __future__ import annotations

import argparse
from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.workspace_policy import WorkspacePolicy


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _ws(mode: str | None) -> SimpleNamespace:
    return SimpleNamespace(mode=mode)


def test_prod_mode_makes_require_flags_implicit() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws("prod"), _args())
    assert p.require_attio is True
    assert p.require_gmail is True
    assert p.require_anthropic is True
    assert p.require_ready_to_send is True
    # Permission gates stay strict in prod.
    assert p.allow_example_domains is False
    assert p.allow_fixture_data is False


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


def test_dev_mode_default_skips_missing_integrations() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws("dev"), _args())
    assert p.require_attio is False
    assert p.integration_skip_or_fail(system="attio") == "skip"
    assert p.integration_skip_or_fail(system="gmail") == "skip"


def test_explicit_require_flag_overrides_dev_mode_default() -> None:
    p = WorkspacePolicy.from_workspace_and_args(
        _ws("dev"), _args(require_attio=True),
    )
    assert p.require_attio is True
    assert p.integration_skip_or_fail(system="attio") == "fail"
    # Untouched integrations still skip.
    assert p.integration_skip_or_fail(system="gmail") == "skip"


def test_include_not_ready_opts_out_of_prod_require_ready() -> None:
    p = WorkspacePolicy.from_workspace_and_args(
        _ws("prod"), _args(include_not_ready=True),
    )
    assert p.require_ready_to_send is False
    # The other prod defaults still hold.
    assert p.require_attio is True


def test_missing_mode_defaults_to_dev() -> None:
    p = WorkspacePolicy.from_workspace_and_args(_ws(None), _args())
    assert p.mode == "dev"
    assert p.require_attio is False


def test_missing_args_attribute_defaults_to_false() -> None:
    """Stages only declare the flags they accept; the policy fills in
    False for anything missing on the Namespace."""
    args = _args()  # zero flags
    p = WorkspacePolicy.from_workspace_and_args(_ws("dev"), args)
    assert p.allow_example_domains is False
    assert p.allow_fixture_data is False
    assert p.allow_unknown_partner_ids is False
