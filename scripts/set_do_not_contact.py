"""Mark a partner as do_not_contact (#441/#684).

The flag is consulted by Stage 6 (as a major_kill component) and
Stage 7 (routes outreach_status='do_not_contact'). Distinct from
warm-path: warm-path means "use the warm channel"; do_not_contact
means "use NO channel".

Examples:
  uv run scripts/set_do_not_contact.py --partner-id NAME \\
      --reason "conflict of interest with existing investor"
  uv run scripts/set_do_not_contact.py --partner-id NAME --clear
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, partners
from core.runs import RunLogger

STAGE = "set_do_not_contact"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set/clear do_not_contact on a partner.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--reason", help="Justification recorded with the flag.")
    g.add_argument("--clear", action="store_true",
                   help="Clear do_not_contact + reason.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    set_flag = not args.clear

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            existing = conn.execute(
                select(
                    partners.c.partner_id, partners.c.name,
                    partners.c.do_not_contact,
                ).where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[dnc] partner {args.partner_id!r} not found")
                run.failed = 1
                run.log_error(args.partner_id, "not_found", "no such partner")
                return 2
            old = bool(existing.do_not_contact)
            conn.execute(
                partners.update()
                .where(partners.c.partner_id == args.partner_id)
                .values(
                    do_not_contact=set_flag,
                    do_not_contact_reason=args.reason if set_flag else None,
                    last_updated=_now(),
                )
            )
        msg = (
            f"{args.partner_id} ({existing.name}): do_not_contact "
            f"{old} -> {set_flag} ({(args.reason or '-')!r})"
        )
        print(f"[dnc] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
