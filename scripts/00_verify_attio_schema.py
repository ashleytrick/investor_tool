"""Stage 0: verify the workspace's Attio schema before any sync runs.

Reads expected attribute slugs from clients/{workspace}/config/attio.yaml and
confirms each exists on the live Attio object via
GET /v2/objects/{object}/attributes. If any are missing, lists them and exits
non-zero. NEVER auto-creates attributes.

If the workspace has no attio.yaml or no ATTIO_API_KEY, exits 0 with a clear
skip message (the CSV path runs without Attio).

Run: uv run scripts/00_verify_attio_schema.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.attio_client import AttioClient, AttioError, AttioNotConfigured
from core.config_loader import add_workspace_arg
from core.stage_runner import stage_run

STAGE = "00_verify_attio_schema"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 0 Attio schema verification.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--allow-skip", action="store_true",
        help="Treat 'attio.yaml present but ATTIO_API_KEY missing' as a "
             "clean skip (exit 0). Default is to FAIL: if the operator "
             "explicitly invoked schema verification on a workspace that "
             "intends to use Attio, the key absence is a configuration "
             "problem, not a no-op.",
    )
    args = parser.parse_args()
    with stage_run(
        args, stage=STAGE,
        # --allow-skip lets us bypass the preflight require_attio so
        # the in-body skip-vs-fail check below is the authority.
        require_attio=bool(_attio_present(args)) and not args.allow_skip,
        require_llm=False,
    ) as ctx:
        ws, run = ctx.ws, ctx.run
        cfg = ws.attio or {}
        attio_cfg = cfg.get("attio") or cfg
        if not attio_cfg:
            print(f"[stage 0] no attio.yaml in workspace {ws.name!r}; skipping")
            with run.attempt():
                run.skip("no attio.yaml")
            return ctx.exit_code
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            if args.allow_skip:
                print(f"[stage 0] {exc}; --allow-skip in effect, skipping")
                with run.attempt():
                    run.skip(f"key missing (--allow-skip): {exc}")
                return ctx.exit_code
            # Default exit code preserved (OPERATIONAL_FAILURE = 2) so
            # existing test/cron contracts hold. A follow-up commit
            # will reclassify safety-gate refusals as refuse_unsafe()
            # (= 3) workspace-wide.
            ctx.refuse(
                f"attio.yaml configured but ATTIO_API_KEY not "
                f"resolvable ({exc}). Set the key (or re-run with "
                f"--allow-skip if you want this to be a no-op in cron)."
            )
            print(f"[stage 0] REFUSED: see runs.error_summary")
            return ctx.exit_code

        objects = attio_cfg.get("objects") or {"funds": "companies", "partners": "people"}
        # Finding 40: base attributes Stage 8 actually writes must exist too.
        base_attrs = {
            objects["funds"]: {"name", "domains"},
            objects["partners"]: {"name", "email_addresses", "company"},
        }
        expected = {
            objects["funds"]: (
                set((attio_cfg.get("fund_attributes") or {}).values())
                | base_attrs[objects["funds"]]
            ),
            objects["partners"]: (
                set((attio_cfg.get("partner_attributes") or {}).values())
                | base_attrs[objects["partners"]]
            ),
        }
        for object_slug, want in expected.items():
            with run.attempt():
                try:
                    have = client.attribute_slugs(object_slug)
                except AttioError as exc:
                    print(f"[stage 0] FAIL {object_slug}: {exc}")
                    run.fail(object_slug, "attio_error", str(exc))
                    continue
                missing = sorted(s for s in want if s and s not in have)
                if missing:
                    print(
                        f"[stage 0] FAIL {object_slug}: missing "
                        f"{len(missing)} attribute(s): {missing}"
                    )
                    run.fail(
                        object_slug, "missing_attributes",
                        f"{len(missing)} missing: {missing}",
                    )
                else:
                    print(
                        f"[stage 0] ok  {object_slug}: {len(want)} expected "
                        f"attributes present"
                    )
                    run.succeed()
        client.close()
    return ctx.exit_code


def _attio_present(args) -> bool:
    """Tiny helper -- can't call ws.attio before stage_run runs, so peek
    at the workspace config dir directly. Returns True iff
    config/attio.yaml exists with non-empty content (which mirrors how
    Workspace.attio gets populated)."""
    from core.config_loader import load_workspace
    try:
        ws = load_workspace(getattr(args, "workspace", None))
        return bool(ws.attio)
    except Exception:  # noqa: BLE001 - workspace errors surface via stage_run
        return False


if __name__ == "__main__":
    raise SystemExit(main())
