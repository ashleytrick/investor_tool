"""List Stage 3 fuzzy matches that were ambiguous (top two candidates
within 5% of each other). Operators audit them via this CLI and resolve
via scripts/resolve_ambiguous_match.py.

Examples:
  uv run scripts/list_ambiguous_matches.py --workspace clients/foo
  uv run scripts/list_ambiguous_matches.py --workspace clients/foo \\
      --unresolved-only --json
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
from core.db import ambiguous_matches, get_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="List ambiguous Stage 3 matches.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--unresolved-only", action="store_true",
        help="Hide rows already resolved via resolve_ambiguous_match.py.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_ambiguous_matches")

    out: list[dict] = []
    with engine.begin() as conn:
        stmt = select(ambiguous_matches)
        if args.unresolved_only:
            stmt = stmt.where(ambiguous_matches.c.resolved_id.is_(None))
        for r in conn.execute(stmt.order_by(desc(ambiguous_matches.c.match_id))):
            try:
                cands = json.loads(r.candidates) if r.candidates else []
            except (TypeError, ValueError):
                cands = []
            out.append({
                "match_id": r.match_id,
                "entity_type": r.entity_type,
                "raw_name": r.raw_name,
                "source_url": r.source_url,
                "candidates": cands,
                "chosen_id": r.chosen_id,
                "chosen_score": r.chosen_score,
                "resolved_id": r.resolved_id,
                "resolved_by": r.resolved_by,
                "resolved_at": str(r.resolved_at) if r.resolved_at else None,
                "resolution_note": r.resolution_note,
            })

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    print()
    print(f"== ambiguous matches ({len(out)}) ==")
    for r in out:
        status = "RESOLVED" if r["resolved_id"] else "OPEN"
        print(
            f"  #{r['match_id']:4d} [{status:8s}] {r['entity_type']:7s} "
            f"raw={r['raw_name']!r}  chose={r['chosen_id']!r} "
            f"({r['chosen_score']})  src={r['source_url']!r}"
        )
        for c in r["candidates"][:3]:
            print(
                f"     candidate: {c['id']!r:35s} score={c['score']}  "
                f"name={c['name']!r}"
            )
        if r["resolved_id"]:
            print(
                f"     -> resolved to {r['resolved_id']!r} by "
                f"{r['resolved_by']!r} at {r['resolved_at']} "
                f"note={r['resolution_note']!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
