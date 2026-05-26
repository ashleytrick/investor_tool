"""Show the audit trail of manual_override actions for a partner
(Slice 18a).

manual_override.py used to pack reasons into a single namespaced
string on partner_score_summaries.manual_override_reason
("score: ...; rec: ...; warm: ..."). The legacy field is still
written for back-compat reads, but the new canonical audit source is
the append-only `manual_override_events` table.

This CLI surfaces every event (set / clear) for a partner -- who did
it, when, with what justification, and (for --recommend) what new
value they pinned. Useful for "why is this partner stuck at
recommended_to_send=no?" investigations months after the fact.

Examples:
  uv run scripts/list_override_events.py --workspace clients/{name} \\
      --partner-id <pid>
  uv run scripts/list_override_events.py --workspace clients/{name} \\
      --partner-id <pid> --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, manual_override_events


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List manual_override audit events for a partner.",
    )
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_override_events")

    with engine.begin() as conn:
        rows = list(conn.execute(
            select(manual_override_events)
            .where(manual_override_events.c.partner_id == args.partner_id)
            .order_by(manual_override_events.c.event_id)
        ))

    if args.json:
        print(json.dumps([
            {
                "event_id": r.event_id,
                "kind": r.kind,
                "action": r.action,
                "reason": r.reason,
                "new_value": r.new_value,
                "actor": r.actor,
                "at": r.at.isoformat() if r.at else None,
            }
            for r in rows
        ], indent=2))
        return 0

    if not rows:
        print(
            f"[override_events] no events for partner_id={args.partner_id!r}"
        )
        return 0

    print(
        f"== {len(rows)} event(s) for partner_id={args.partner_id} =="
    )
    for r in rows:
        val = f" -> {r.new_value!r}" if r.new_value else ""
        print(
            f"  #{r.event_id:>4} {r.at.isoformat() if r.at else '?':<20} "
            f"{r.kind:<6} {r.action:<5}{val}  by={r.actor!r}  "
            f"reason={(r.reason or '-')!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
