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

If the operator instead wants to merge a provisional fund INTO an
existing canonical fund (different fund_id), use
`scripts/bulk_reattribute.py` -- it moves all deal_attributions and
then the operator can drop the provisional row.
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
    promote_provisional_fund,
    promote_provisional_partner,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine, partners
from core.runs import RunLogger

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
    parser.add_argument("--actor", default=None,
                        help="Operator id (defaults to $USER).")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    actor = args.actor or os.environ.get("USER") or "unknown"

    if args.list:
        return _list_provisional(engine)

    if args.fund_id and (args.new_title or args.new_linkedin):
        print("[promote_provisional] --new-title / --new-linkedin are partner-only")
        return 1
    if args.partner_id and args.new_domain:
        print("[promote_provisional] --new-domain is fund-only")
        return 1

    with RunLogger(engine, ws.name, STAGE) as run:
        try:
            if args.fund_id:
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
            return 0
        except PromotionError as exc:
            print(f"[promote_provisional] REFUSED: {exc}")
            run.failed = 1
            run.log_error(args.fund_id or args.partner_id or "?",
                          "promotion_refused", str(exc))
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
