"""Operator CLI: move every deal_attributions row from one fund_id
to another.

Use cases:
  - Stage 3 created a provisional fund the operator now wants to merge
    into the existing canonical fund row.
  - Two funds were created by accident (different domains, same VC)
    and need to be consolidated.
  - The wrong fund was attributed for a whole vintage of announcements.

Every moved row's `match_status` is rewritten to `confirmed` and
`matched_by` to `manual` -- the operator is the authoritative source.
With `--also-remap-partners`, attributed partner ids are looked up by
name in the destination fund and rewritten; partners that can't be
matched are nulled out (and listed in the summary) so Stage 6 doesn't
credit the wrong fund's partner with the deal.

Examples:
  uv run scripts/bulk_reattribute.py --workspace clients/{name} \\
      --from-fund-id acme-capital.provisional --to-fund-id acme.example \\
      --also-remap-partners --dry-run
  uv run scripts/bulk_reattribute.py --workspace clients/{name} \\
      --from-fund-id acme-capital.provisional --to-fund-id acme.example \\
      --also-remap-partners

After the move, Stage 3's `recompute_fund_activity` is rerun so both
funds' `last_known_activity_date` / `is_active` flags reflect the new
attribution. The source fund is NOT deleted automatically; the operator
runs `set_fund_inactive.py` (or removes the row directly) when ready.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.attribution.promotion import (
    PromotionError,
    bulk_reattribute_deals,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine
from core.runs import RunLogger

STAGE = "bulk_reattribute"


def _recompute(engine) -> None:
    """Lazy import to avoid pulling Stage 3's full import graph when
    this script is run."""
    from importlib import util as _util
    spec = _util.spec_from_file_location(
        "_stage3_mod",
        pathlib.Path(__file__).resolve().parent / "03_mine_activity.py",
    )
    mod = _util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    mod.recompute_fund_activity(engine)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move deal_attributions rows from one fund to another.",
    )
    add_workspace_arg(parser)
    parser.add_argument("--from-fund-id", required=True)
    parser.add_argument("--to-fund-id", required=True)
    parser.add_argument(
        "--also-remap-partners", action="store_true",
        help="Look up attributed_partner_id by name in the destination "
             "fund and rewrite. Partners without a name match are nulled.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--actor", default=None,
                        help="Operator id (defaults to $USER).")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    actor = args.actor or os.environ.get("USER") or "unknown"

    with RunLogger(engine, ws.name, STAGE) as run:
        try:
            result = bulk_reattribute_deals(
                engine,
                from_fund_id=args.from_fund_id,
                to_fund_id=args.to_fund_id,
                actor=actor,
                also_remap_partners=args.also_remap_partners,
                dry_run=args.dry_run,
            )
        except PromotionError as exc:
            print(f"[bulk_reattribute] REFUSED: {exc}")
            run.failed = 1
            run.log_error(
                f"{args.from_fund_id}->{args.to_fund_id}",
                "reattribute_refused", str(exc),
            )
            return 2

        prefix = "DRY RUN " if result.dry_run else ""
        print(
            f"[bulk_reattribute] {prefix}{result.deals_moved} deal(s) moved "
            f"{args.from_fund_id} -> {args.to_fund_id}; "
            f"partners_remapped={result.partners_remapped} "
            f"partners_orphaned={len(result.partners_orphaned)}"
        )
        if result.partners_orphaned:
            print(
                f"[bulk_reattribute] orphaned partners (cleared from rows): "
                f"{sorted(set(result.partners_orphaned))}"
            )

        if not result.dry_run:
            _recompute(engine)
            run.note(
                f"reattributed {result.deals_moved} deal(s) "
                f"{args.from_fund_id} -> {args.to_fund_id} by {actor}; "
                f"remapped={result.partners_remapped} "
                f"orphaned={len(result.partners_orphaned)}"
            )
        run.processed = result.deals_moved
        run.succeeded = result.deals_moved
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
