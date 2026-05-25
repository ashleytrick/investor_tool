"""Backfill deal_attributions rows whose raw_lead_investor or
raw_attributed_partners didn't resolve at original Stage 3 time but
DO resolve now (e.g. Stage 2 has since discovered the partner).

The Stage 3 LLM is NOT re-invoked; this script only re-runs the local
match logic against the existing raw names. Idempotent.

Examples:
  uv run scripts/backfill_attributions.py --workspace clients/foo --dry-run
  uv run scripts/backfill_attributions.py --workspace clients/foo
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import deal_attributions, funds, get_engine, partners
from core.ids import normalize_name, partner_id_for
from core.runs import RunLogger

STAGE = "backfill_attributions"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Stage 3 attributions from raw names.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with engine.begin() as conn:
        fund_rows = list(conn.execute(
            select(funds.c.fund_id, funds.c.name, funds.c.domain),
        ))
        partner_rows = list(conn.execute(select(partners.c.partner_id)))
    funds_by_name = {normalize_name(r.name): r.fund_id for r in fund_rows}
    fund_id_to_domain = {r.fund_id: r.domain for r in fund_rows}
    known_partner_ids = {r.partner_id for r in partner_rows}

    # Pull rows where attribution is incomplete (no fund_id OR no
    # partner_id) but raw names exist.
    candidates: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(
            select(deal_attributions).where(
                (deal_attributions.c.lead_fund_id.is_(None))
                | (deal_attributions.c.attributed_partner_id.is_(None))
            )
        ):
            if not (r.raw_lead_investor or r.raw_attributed_partners):
                continue
            candidates.append({
                "deal_id": r.deal_id,
                "source_url": r.source_url,
                "lead_fund_id": r.lead_fund_id,
                "attributed_partner_id": r.attributed_partner_id,
                "raw_lead_investor": r.raw_lead_investor,
                "raw_attributed_partners": r.raw_attributed_partners,
            })

    with RunLogger(engine, ws.name, STAGE) as run:
        run.processed = len(candidates)
        backfilled_funds = 0
        backfilled_partners = 0
        for c in candidates:
            updates: dict = {}
            # Try to resolve the lead fund.
            if c["lead_fund_id"] is None and c["raw_lead_investor"]:
                key = normalize_name(c["raw_lead_investor"])
                if key in funds_by_name:
                    updates["lead_fund_id"] = funds_by_name[key]
                    backfilled_funds += 1
            # Try to resolve the partner.
            if c["attributed_partner_id"] is None and c["raw_attributed_partners"]:
                try:
                    raw_partners = json.loads(c["raw_attributed_partners"])
                except (TypeError, ValueError):
                    raw_partners = []
                for ap in raw_partners:
                    name = ap.get("name") if isinstance(ap, dict) else None
                    fund = ap.get("fund") if isinstance(ap, dict) else None
                    if not name or not fund:
                        continue
                    fid = funds_by_name.get(normalize_name(fund))
                    if not fid:
                        continue
                    domain = fund_id_to_domain.get(fid)
                    if not domain:
                        continue
                    pid = partner_id_for(domain, name)
                    if pid in known_partner_ids:
                        updates["attributed_partner_id"] = pid
                        # If we also resolved the lead fund through the
                        # partner's fund, set it too.
                        if "lead_fund_id" not in updates and c["lead_fund_id"] is None:
                            updates["lead_fund_id"] = fid
                        backfilled_partners += 1
                        break
            if not updates:
                continue
            if args.dry_run:
                print(
                    f"[backfill] DRY: deal_id={c['deal_id']} "
                    f"src={c['source_url']!r} updates={updates}"
                )
                continue
            with engine.begin() as conn:
                conn.execute(
                    deal_attributions.update()
                    .where(deal_attributions.c.deal_id == c["deal_id"])
                    .values(**updates)
                )
            run.note(
                f"backfilled deal_id={c['deal_id']} updates={updates}"
            )
        run.succeeded = backfilled_funds + backfilled_partners
        print(
            f"[backfill] candidates={len(candidates)} "
            f"backfilled_funds={backfilled_funds} "
            f"backfilled_partners={backfilled_partners} "
            f"{'(dry-run)' if args.dry_run else ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
