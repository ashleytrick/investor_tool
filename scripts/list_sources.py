"""List the canonical `sources` registry (Slice 18b).

Every distinct URL the pipeline has touched lands here with a stable
source_id, a category hint, and first/last seen timestamps. Useful
for: spotting URL drift ("we have two rows that mean the same
thing"), auditing scrape coverage ("how many partner-content URLs
have we seen?"), and -- once future slices migrate the loose
source_url columns -- the FK reference any consumer joins to.

Examples:
  uv run scripts/list_sources.py --workspace clients/{name}
  uv run scripts/list_sources.py --workspace clients/{name} --json
  uv run scripts/list_sources.py --workspace clients/{name} \\
      --source-type partner_content
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, sources


def main() -> int:
    parser = argparse.ArgumentParser(description="List the sources registry.")
    add_workspace_arg(parser)
    parser.add_argument("--source-type", default=None,
                        help="Filter to a single source_type.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_sources")

    with engine.begin() as conn:
        stmt = select(sources).order_by(desc(sources.c.last_seen_at))
        if args.source_type:
            stmt = stmt.where(sources.c.source_type == args.source_type)
        rows = list(conn.execute(stmt))

    if args.json:
        print(json.dumps([
            {
                "source_id": r.source_id,
                "source_url": r.source_url,
                "source_type": r.source_type,
                "first_seen_at": r.first_seen_at.isoformat()
                                  if r.first_seen_at else None,
                "last_seen_at": r.last_seen_at.isoformat()
                                if r.last_seen_at else None,
            }
            for r in rows
        ], indent=2))
        return 0

    if not rows:
        print("[sources] registry is empty")
        return 0
    print(f"== {len(rows)} source(s) ==")
    for r in rows:
        print(
            f"  #{r.source_id:>5} {(r.source_type or '?'):<28} "
            f"last_seen={r.last_seen_at.isoformat() if r.last_seen_at else '?'}  "
            f"{r.source_url}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
