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
from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import get_engine
from core.runs import RunLogger
from core.validate_config import preflight_or_exit

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

    ws = load_workspace(args.workspace)
    # When --allow-skip is set, the operator explicitly accepts that a
    # missing key is a no-op. Don't let preflight refuse on that very
    # condition; the in-body skip-vs-fail check below is the authority.
    preflight_or_exit(
        ws, stage=STAGE,
        require_attio=bool(ws.attio) and not args.allow_skip,
    )
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    cfg = ws.attio or {}
    attio_cfg = cfg.get("attio") or cfg

    with RunLogger(engine, ws.name, STAGE) as run:
        if not attio_cfg:
            # No attio.yaml at all -> clean no-op (this workspace is CSV-only).
            print(f"[stage 0] no attio.yaml in workspace {ws.name!r}; skipping")
            run.skipped = 1
            return 0
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            # attio.yaml is configured but the key is missing. Default to
            # FAIL so the operator who ran Stage 0 expecting a real check
            # doesn't get a misleading green light. --allow-skip restores
            # the prior cron-friendly behavior.
            if args.allow_skip:
                print(f"[stage 0] {exc}; --allow-skip in effect, skipping")
                run.skipped = 1
                return 0
            msg = (
                f"REFUSED: attio.yaml is configured for this workspace but "
                f"ATTIO_API_KEY is not resolvable ({exc}). Set the key (or "
                f"re-run with --allow-skip if you want this to be a no-op "
                f"in cron)."
            )
            print(f"[stage 0] {msg}")
            run.note(msg)
            run.failed = 1
            return 2

        objects = attio_cfg.get("objects") or {"funds": "companies", "partners": "people"}
        # Finding 40: base attributes Stage 8 actually writes (name, domains
        # for companies; name, email_addresses, company link for people)
        # must exist too -- the previous check only validated custom-attr
        # slugs from attio.yaml, so a misconfigured object could fail at
        # the first sync with a confusing API error.
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
        all_ok = True
        for object_slug, want in expected.items():
            run.processed += 1
            try:
                have = client.attribute_slugs(object_slug)
            except AttioError as exc:
                print(f"[stage 0] FAIL {object_slug}: {exc}")
                run.log_error(object_slug, "attio_error", str(exc))
                all_ok = False
                run.failed += 1
                continue
            missing = sorted(s for s in want if s and s not in have)
            if missing:
                print(
                    f"[stage 0] FAIL {object_slug}: missing {len(missing)} "
                    f"attribute(s): {missing}"
                )
                run.failed += 1
                all_ok = False
            else:
                print(
                    f"[stage 0] ok  {object_slug}: {len(want)} expected attributes present"
                )
                run.succeeded += 1
        client.close()
        return 0 if all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
