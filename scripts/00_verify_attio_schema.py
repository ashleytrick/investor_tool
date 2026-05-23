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

STAGE = "00_verify_attio_schema"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 0 Attio schema verification.")
    add_workspace_arg(parser)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    cfg = ws.attio or {}
    attio_cfg = cfg.get("attio") or cfg

    with RunLogger(engine, ws.name, STAGE) as run:
        if not attio_cfg:
            print(f"[stage 0] no attio.yaml in workspace {ws.name!r}; skipping")
            run.skipped = 1
            return 0
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            print(f"[stage 0] {exc}; skipping")
            run.skipped = 1
            return 0

        objects = attio_cfg.get("objects") or {"funds": "companies", "partners": "people"}
        expected = {
            objects["funds"]: set((attio_cfg.get("fund_attributes") or {}).values()),
            objects["partners"]: set((attio_cfg.get("partner_attributes") or {}).values()),
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
