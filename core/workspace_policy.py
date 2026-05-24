"""Workspace mode policy (Refactor item 10).

The pipeline accumulated a growing pile of operator flags --
`--allow-fixture-mode`, `--allow-example-domains`,
`--allow-unknown-partner-ids`, `--include-not-ready`, `--require-gmail`,
`--require-attio` -- each with its own per-stage prod-mode default
inline. The same `args.X or ws.mode == "prod"` pattern repeats across
Stages 7, 8 and the Gmail draft script, and the same `ws.mode ==
"fixture" and not args.allow_fixture_mode` refusal repeats in Stage 8
and gmail.

This module centralizes that derivation. Stages build a
`WorkspacePolicy` from the workspace + parsed args once and then ask
the policy for the per-decision answer:

    policy = WorkspacePolicy.from_workspace_and_args(ws, args)
    if policy.refuse_fixture_data():
        ctx.refuse_unsafe("...")
    if not policy.allow_example_domains:
        # filter out fixture domains
    if policy.require_attio:
        # treat missing config as fail, not skip

Modes
-----
- "prod": real workspace pushing to real Attio + Gmail. Strict
  defaults: require all integrations, refuse example domains, refuse
  fixture data, require partner_id integrity, require recommended
  drafts only for Stage 8.
- "fixture": shipped test_workspace. Permissive on data shape; refuses
  to sync to real CRM unless --allow-fixture-mode.
- "dev" (default for any non-prod, non-fixture mode): permissive on
  integrations being missing (skip rather than fail), but still rejects
  example domains unless --allow-example-domains and still treats
  unknown CSV partner_ids strictly.

Each constructor parameter has a CLI flag counterpart on the stage
that uses it; this object's job is to apply the mode defaults
consistently, not to define the flag set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkspacePolicy:
    mode: str
    # Integration requirements. True means missing config is a HARD
    # FAILURE; False means missing config produces a clean skip.
    require_attio: bool
    require_gmail: bool
    require_anthropic: bool
    # Stage 8 only: true means filter to recommended_to_send + qa=pass.
    require_ready_to_send: bool
    # Permission gates that escape strict defaults:
    allow_example_domains: bool
    allow_fixture_data: bool  # Stage 8 / gmail mode=fixture override
    allow_unknown_partner_ids: bool  # Stage 4 / record_outcome CSV strictness

    @classmethod
    def from_workspace_and_args(cls, ws: Any, args: Any) -> "WorkspacePolicy":
        """Build a policy from a workspace + argparse Namespace. Args
        are read with getattr+default so each stage only needs to
        define the flags it actually accepts; missing flags fall
        through to the mode-driven default."""
        mode = (getattr(ws, "mode", None) or "dev").lower()
        is_prod = mode == "prod"

        def opt(name: str, default: bool = False) -> bool:
            return bool(getattr(args, name, default))

        return cls(
            mode=mode,
            # Each --require-X flag is opt-in for non-prod; prod-mode
            # makes it implicit. So a prod workspace cron that omits
            # the flag still gets the safe behavior.
            require_attio=opt("require_attio") or is_prod,
            require_gmail=opt("require_gmail") or is_prod,
            require_anthropic=opt("require_anthropic") or is_prod,
            # Stage 8: prod requires ready+qa-pass unless the operator
            # explicitly opted out via --include-not-ready.
            require_ready_to_send=(
                opt("require_ready_to_send")
                or (is_prod and not opt("include_not_ready"))
            ),
            # Example-domain permission: prod refuses unless flag set;
            # fixture/dev mode also defaults to refusing so an operator
            # who edited a real workspace from a fixture copy doesn't
            # silently ship .example URLs.
            allow_example_domains=opt("allow_example_domains"),
            # Fixture-data gate: Stage 8 + gmail refuse to write to
            # real systems from a mode=fixture workspace unless
            # --allow-fixture-mode opts in.
            allow_fixture_data=opt("allow_fixture_mode"),
            # CSV strictness: opt-in to LENIENT mode -- unknown
            # partner_id in operator CSVs becomes a soft log rather
            # than a fail bump.
            allow_unknown_partner_ids=opt("allow_unknown_partner_ids"),
        )

    # ----- Decision helpers -----
    #
    # These wrap the common branches so the call sites stay readable and
    # the policy intent shows up in the trace.

    def refuses_fixture_data(self) -> bool:
        """Stage 8 + gmail: should we refuse to write because the
        workspace is mode=fixture and the operator did not opt in?"""
        return self.mode == "fixture" and not self.allow_fixture_data

    def integration_skip_or_fail(self, *, system: str) -> str:
        """Return 'fail' when a missing `system` config should be a hard
        failure (prod or explicit --require-X); 'skip' otherwise.
        `system` is one of 'attio', 'gmail', 'anthropic'.
        """
        required = {
            "attio": self.require_attio,
            "gmail": self.require_gmail,
            "anthropic": self.require_anthropic,
        }.get(system, False)
        return "fail" if required else "skip"
