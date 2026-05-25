"""Operator CLI: confirm a provisional Stage 3 fund or partner.

Stage 3 with --allow-provisional creates rows flagged
`is_provisional=TRUE` for funds/partners named in announcements that
the local DB hadn't yet researched. Once the operator has verified
the entity is real (Stage 2 enrichment ran successfully, the LinkedIn
profile checks out, etc.), this script clears the flag so downstream
filters stop treating the row as tentative.

For a single fund row -- typical when Stage 2 finally enriched the
record and the operator wants to drop the provisional flag in place:

  uv run scripts/promote_provisional.py --workspace clients/{name} \\
      --fund-id <id> [--new-name "Acme Capital"] [--new-domain acme.com]

For a single partner:

  uv run scripts/promote_provisional.py --workspace clients/{name} \\
      --partner-id <id> [--new-name "..."] [--new-title "..."] \\
      [--new-linkedin <url>]

To list all provisional rows currently in the workspace:

  uv run scripts/promote_provisional.py --workspace clients/{name} --list

To merge a provisional fund INTO an existing canonical fund
(different fund_id), pass `--merge-into <real_fund_id>`. This:
  1. Moves every deal_attributions row from the provisional fund to
     the target via core.attribution.promotion.bulk_reattribute_deals
     (sets match_status=confirmed + matched_by=manual).
  2. Marks the provisional fund inactive (is_active=False) and leaves
     the row in place for audit. Operator can delete it separately
     via SQL or set_fund_inactive.
  3. Recomputes fund_activity so the destination's
     last_known_activity_date reflects the new attributions.

  uv run scripts/promote_provisional.py --workspace clients/{name} \\
      --fund-id <provisional_id> --merge-into <real_fund_id>
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.attribution.promotion import (
    PromotionError,
    bulk_reattribute_deals,
    promote_provisional_fund,
    promote_provisional_partner,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine, partners
from core.operator_command import operator_command_run

STAGE = "promote_provisional"


def _list_provisional(engine) -> int:
    with engine.begin() as conn:
        fund_rows = list(conn.execute(
            select(funds.c.fund_id, funds.c.name, funds.c.domain)
            .where(funds.c.is_provisional == True)  # noqa: E712
            .order_by(funds.c.name)
        ))
        partner_rows = list(conn.execute(
            select(partners.c.partner_id, partners.c.name, partners.c.fund_id)
            .where(partners.c.is_provisional == True)  # noqa: E712
            .order_by(partners.c.name)
        ))
    print()
    print(f"== provisional funds ({len(fund_rows)}) ==")
    for r in fund_rows:
        print(f"  {r.fund_id}  name={r.name!r}  domain={r.domain!r}")
    print()
    print(f"== provisional partners ({len(partner_rows)}) ==")
    for r in partner_rows:
        print(f"  {r.partner_id}  name={r.name!r}  fund={r.fund_id!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear is_provisional on a fund or partner row.",
    )
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--fund-id", default=None)
    g.add_argument("--partner-id", default=None)
    parser.add_argument("--new-name", default=None)
    parser.add_argument("--new-domain", default=None,
                        help="Fund only: replaces the synthetic .provisional domain.")
    parser.add_argument("--new-title", default=None,
                        help="Partner only.")
    parser.add_argument("--new-linkedin", default=None,
                        help="Partner only.")
    parser.add_argument(
        "--merge-into", default=None,
        help="Fund only: merge this provisional fund into the given "
             "canonical fund_id. Moves every deal_attributions row "
             "+ deactivates the provisional fund.",
    )
    parser.add_argument(
        "--also-remap-partners", action="store_true",
        help="With --merge-into: also remap attributed partner ids by "
             "name into the destination fund's roster (unmatchable "
             "partners are nulled).",
    )
    parser.add_argument("--actor", default=None,
                        help="Operator id (defaults to $USER).")
    args = parser.parse_args()
    actor = args.actor or os.environ.get("USER") or "unknown"

    # --list is read-only -- skip the lock + backup overhead so an
    # operator who wants to inspect can do so even mid-pipeline.
    if args.list:
        ws = load_workspace(args.workspace)
        engine = get_engine(ws.db_url)
        print_banner(ws, stage=STAGE)
        return _list_provisional(engine)

    if args.fund_id and (args.new_title or args.new_linkedin):
        print("[promote_provisional] --new-title / --new-linkedin are partner-only")
        return 1
    if args.partner_id and (args.new_domain or args.merge_into or args.also_remap_partners):
        print(
            "[promote_provisional] --new-domain / --merge-into / "
            "--also-remap-partners are fund-only"
        )
        return 1
    if args.merge_into and (args.new_name or args.new_domain):
        print(
            "[promote_provisional] --merge-into is mutually exclusive "
            "with --new-name / --new-domain (the destination fund "
            "carries its own identity; nothing to rename on the "
            "merged-away source)"
        )
        return 1

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        try:
            if args.fund_id and args.merge_into:
                # Loose end from Slice 12: merge a provisional fund
                # into a real one. Moves every deal_attributions row,
                # then deactivates the source so it stops appearing in
                # the active-fund list. We re-run Stage 3's
                # recompute_fund_activity inline so the destination's
                # last_known_activity_date / is_active reflect the
                # new attributions.
                merge_result = bulk_reattribute_deals(
                    engine,
                    from_fund_id=args.fund_id,
                    to_fund_id=args.merge_into,
                    actor=actor,
                    also_remap_partners=args.also_remap_partners,
                )
                # Mark the source fund inactive + clear provisional.
                from sqlalchemy import update as _update
                from datetime import datetime as _dt, timezone as _tz
                with engine.begin() as conn:
                    conn.execute(
                        _update(funds)
                        .where(funds.c.fund_id == args.fund_id)
                        .values(
                            is_active=False,
                            is_provisional=False,
                            last_updated=_dt.now(_tz.utc),
                        )
                    )
                # Inline recompute so destination's activity stamp
                # picks up the moved deals immediately.
                from importlib import util as _util
                spec = _util.spec_from_file_location(
                    "_stage3_mod",
                    pathlib.Path(__file__).resolve().parent / "03_mine_activity.py",
                )
                mod = _util.module_from_spec(spec)
                assert spec.loader
                spec.loader.exec_module(mod)
                mod.recompute_fund_activity(engine)
                print(
                    f"[promote_provisional] merged fund "
                    f"{args.fund_id} -> {args.merge_into}: "
                    f"{merge_result.deals_moved} deal(s) reattributed, "
                    f"{merge_result.partners_remapped} partner(s) remapped, "
                    f"{len(merge_result.partners_orphaned)} orphaned. "
                    f"Source fund deactivated."
                )
                run.note(
                    f"merged provisional fund {args.fund_id} into "
                    f"{args.merge_into} by {actor}: "
                    f"{merge_result.deals_moved} deals moved"
                )
                run.processed = merge_result.deals_moved
                run.succeeded = merge_result.deals_moved
            elif args.fund_id:
                result = promote_provisional_fund(
                    engine,
                    fund_id=args.fund_id,
                    new_name=args.new_name,
                    new_domain=args.new_domain,
                )
                print(
                    f"[promote_provisional] fund {result.fund_id} cleared "
                    f"(name={result.renamed_to!r} domain={result.domain_set_to!r})"
                )
                run.note(
                    f"promoted fund {result.fund_id} by {actor}: "
                    f"name={result.renamed_to!r} domain={result.domain_set_to!r}"
                )
                run.succeeded = 1
                run.processed = 1
            else:
                result = promote_provisional_partner(
                    engine,
                    partner_id=args.partner_id,
                    new_name=args.new_name,
                    new_title=args.new_title,
                    new_linkedin=args.new_linkedin,
                )
                print(
                    f"[promote_provisional] partner {result.partner_id} cleared "
                    f"(name={result.renamed_to!r})"
                )
                run.note(
                    f"promoted partner {result.partner_id} by {actor}: "
                    f"name={result.renamed_to!r}"
                )
                run.succeeded = 1
                run.processed = 1
        except PromotionError as exc:
            print(f"[promote_provisional] REFUSED: {exc}")
            ctx.refuse(str(exc))
            run.log_error(args.fund_id or args.partner_id or "?",
                          "promotion_refused", str(exc))
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
