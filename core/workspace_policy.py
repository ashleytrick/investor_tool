"""Workspace mode policy (Refactor item 10 / Slice 13).

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
    if policy.refuses_external_mutation():
        # dry_run: refuse to call Gmail / Attio mutation APIs

Modes (Slice 13 canonical names)
-------------------------------
- "production": real workspace pushing to real Attio + Gmail. Strict
  defaults: require all integrations, refuse example domains, refuse
  fixture data, require partner_id integrity, require recommended
  drafts only for Stage 8. External mutations allowed.
- "dry_run": real workspace data, integrations are PERMISSIVE on
  missing config (skip rather than fail) AND external mutations are
  REFUSED outright -- Gmail draft push + Stage 8 Attio sync log what
  they would do without calling the API. Reads / local writes (CSV
  exports, DB updates, run audit rows) proceed normally so the
  operator can verify a workspace without touching the outside world.
- "fixture": shipped test_workspace. Permissive on data shape;
  refuses to sync to real CRM unless --allow-fixture-mode. Same
  no-external-mutation semantics as dry_run.

Legacy names "prod" + "dev" are accepted in company.yaml with a
deprecation warning; `core/config_loader.py` normalizes them to the
canonical forms before they reach this module.
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
        # Accept legacy names as aliases so call-sites holding an
        # un-migrated workspace still resolve. The config_loader emits
        # the deprecation warning at load time; we just normalize.
        raw = (getattr(ws, "mode", None) or "dry_run").lower()
        mode = {"prod": "production", "dev": "dry_run"}.get(raw, raw)
        is_production = mode == "production"

        def opt(name: str, default: bool = False) -> bool:
            return bool(getattr(args, name, default))

        return cls(
            mode=mode,
            # Each --require-X flag is opt-in for non-production;
            # production mode makes it implicit so a production cron
            # that omits the flag still gets the safe behavior.
            require_attio=opt("require_attio") or is_production,
            require_gmail=opt("require_gmail") or is_production,
            require_anthropic=opt("require_anthropic") or is_production,
            # Stage 8: production requires ready+qa-pass unless the
            # operator explicitly opted out via --include-not-ready.
            require_ready_to_send=(
                opt("require_ready_to_send")
                or (is_production and not opt("include_not_ready"))
            ),
            # Example-domain permission: production refuses unless
            # flag set; dry_run / fixture also default to refusing so
            # an operator who edited a real workspace from a fixture
            # copy doesn't silently ship .example URLs.
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

    def refuses_external_mutation(self) -> bool:
        """Slice 13: dry_run + fixture must NEVER call an external
        mutation API (Gmail send, Attio create/update) even when
        credentials are configured. Only `production` does the real
        thing.

        Stage 8 and create_gmail_drafts consult this BEFORE touching
        the network so a misconfigured cron against a dry_run
        workspace can't accidentally ship live outreach. Local writes
        (CSV exports, DB updates, run audit rows) are unaffected --
        only mutation calls to outside systems."""
        return self.mode != "production"

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
