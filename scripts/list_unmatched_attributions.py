"""List deal_attributions rows whose raw_lead_investor or raw_attributed_
partners didn't resolve to known funds/partners.

Used to audit "we have these funding announcements naming people we
haven't researched yet" before re-running Stage 2 (enrich those funds)
or scripts/list_partners_for_action.py (find adjacent missing fields).

Examples:
  uv run scripts/list_unmatched_attributions.py --workspace clients/foo
  uv run scripts/list_unmatched_attributions.py --workspace clients/foo --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import deal_attributions, get_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="List unmatched Stage 3 attributions.")
    add_workspace_arg(parser)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_unmatched_attributions")

    unmatched_funds: list[dict] = []
    unmatched_partners: list[dict] = []
    fund_name_counts: Counter[str] = Counter()
    partner_name_counts: Counter[str] = Counter()

    with engine.begin() as conn:
        for r in conn.execute(
            select(
                deal_attributions.c.deal_id,
                deal_attributions.c.source_url,
                deal_attributions.c.raw_lead_investor,
                deal_attributions.c.raw_attributed_partners,
                deal_attributions.c.lead_fund_id,
                deal_attributions.c.attributed_partner_id,
                deal_attributions.c.company,
                deal_attributions.c.announcement_date,
            ).order_by(desc(deal_attributions.c.deal_id))
        ):
            # Unmatched lead fund: raw name recorded but no lead_fund_id.
            if r.raw_lead_investor and not r.lead_fund_id:
                unmatched_funds.append({
                    "deal_id": r.deal_id,
                    "source_url": r.source_url,
                    "raw_lead_investor": r.raw_lead_investor,
                    "company": r.company,
                    "announcement_date": str(r.announcement_date)
                                         if r.announcement_date else None,
                })
                fund_name_counts[r.raw_lead_investor] += 1
            # Unmatched partners: raw_attributed_partners has names but
            # this row has no attributed_partner_id (the row is either
            # skeleton or lead-only-with-named-but-unresolved partners).
            if r.raw_attributed_partners and not r.attributed_partner_id:
                try:
                    raw = json.loads(r.raw_attributed_partners)
                except (TypeError, ValueError):
                    raw = []
                for entry in raw:
                    name = entry.get("name") if isinstance(entry, dict) else None
                    fund = entry.get("fund") if isinstance(entry, dict) else None
                    if name:
                        unmatched_partners.append({
                            "deal_id": r.deal_id,
                            "source_url": r.source_url,
                            "raw_partner": name,
                            "raw_fund": fund,
                            "company": r.company,
                        })
                        partner_name_counts[name] += 1

    if args.json:
        print(json.dumps({
            "unmatched_funds": unmatched_funds,
            "unmatched_partners": unmatched_partners,
            "top_unmatched_fund_names": fund_name_counts.most_common(10),
            "top_unmatched_partner_names": partner_name_counts.most_common(10),
        }, indent=2, default=str))
        return 0

    print()
    print(f"== Unmatched lead funds ({len(unmatched_funds)}) ==")
    for r in unmatched_funds[:50]:
        print(
            f"  deal_id={r['deal_id']:4d}  "
            f"{r['raw_lead_investor']!r:40s} for company "
            f"{r['company']!r} ({r['announcement_date']})"
        )
    if fund_name_counts:
        print()
        print("  TOP unmatched fund names (count >= 1):")
        for name, n in fund_name_counts.most_common(10):
            print(f"    {n}x {name!r}")
    print()
    print(f"== Unmatched attributed partners ({len(unmatched_partners)}) ==")
    for r in unmatched_partners[:50]:
        print(
            f"  deal_id={r['deal_id']:4d}  "
            f"{r['raw_partner']!r:30s} @ {r['raw_fund']!r} "
            f"(company={r['company']!r})"
        )
    if partner_name_counts:
        print()
        print("  TOP unmatched partner names (count >= 1):")
        for name, n in partner_name_counts.most_common(10):
            print(f"    {n}x {name!r}")
    print()
    print(
        "To backfill: re-run Stage 2 (enrich the named funds to discover "
        "their partner team) OR re-run Stage 3 --allow-provisional to "
        "create stub partner/fund rows for the unmatched names."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
