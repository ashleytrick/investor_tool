"""Export partner identity rows to a CSV for Apollo enrichment.

The operator runs Apollo (or any enrichment tool) against this CSV,
gets emails back, then re-imports via import_partner_emails_apollo.py.

CSV columns:
  partner_id    -- workspace-internal id; round-trips intact
  name          -- partner full name
  fund_name     -- the partner's fund (helps Apollo disambiguate)
  fund_domain   -- helps Apollo target the right organization
  linkedin_url  -- if already known
  current_email -- "" when missing; non-empty rows can be skipped
                   in Apollo if the operator only wants to enrich
                   the gaps.

Usage:
  uv run scripts/export_partners_for_apollo.py --workspace clients/{name}
  uv run scripts/export_partners_for_apollo.py --workspace clients/{name} \\
      --missing-only       # exclude rows that already have an email

Lands at clients/{name}/exports/partners_for_apollo.csv (atomic write).
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.csv_export import _atomic_write_csv
from core.db import funds, get_engine, partners


APOLLO_EXPORT_COLUMNS: list[str] = [
    "partner_id",
    "name",
    "fund_name",
    "fund_domain",
    "linkedin_url",
    "current_email",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export partners for Apollo enrichment.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--missing-only", action="store_true",
        help="Skip partners that already have an email on file.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="export_partners_for_apollo")

    with engine.begin() as conn:
        rows_db = list(conn.execute(
            select(
                partners.c.partner_id,
                partners.c.name,
                partners.c.linkedin_url,
                partners.c.email,
                funds.c.name.label("fund_name"),
                funds.c.domain.label("fund_domain"),
            ).join(funds, funds.c.fund_id == partners.c.fund_id)
        ))

    rows: list[dict] = []
    skipped_with_email = 0
    for r in rows_db:
        if args.missing_only and (r.email or "").strip():
            skipped_with_email += 1
            continue
        rows.append({
            "partner_id": r.partner_id,
            "name": r.name or "",
            "fund_name": r.fund_name or "",
            "fund_domain": r.fund_domain or "",
            "linkedin_url": r.linkedin_url or "",
            "current_email": r.email or "",
        })

    out_path = ws.exports_dir / "partners_for_apollo.csv"
    ws.exports_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_csv(out_path, APOLLO_EXPORT_COLUMNS, rows)
    print(
        f"[apollo_export] wrote {len(rows)} partner(s) -> {out_path}"
    )
    if args.missing_only and skipped_with_email:
        print(
            f"[apollo_export] (skipped {skipped_with_email} partner(s) "
            f"that already have an email on file)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
