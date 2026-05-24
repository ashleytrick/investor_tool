"""Edit a partner's warm_path_contact note WITHOUT flipping the
warm_path_available flag (#687).

Use case: the warm-path flag was set previously (via manual_override.py
--warm-path), and now the operator has more detail about WHO has the
intro -- you want to update the contact text but the flag stays on.

manual_override.py --warm-path also sets the flag; this script only
touches the contact text.

Examples:
  uv run scripts/set_warm_path_contact.py --partner-id NAME \\
      --contact "ashley@example.com knows them via Series A board"
  uv run scripts/set_warm_path_contact.py --partner-id NAME --clear
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

STAGE = "set_warm_path_contact"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Edit partners.warm_path_contact without flipping the flag."
    )
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--contact", help="New contact text.")
    g.add_argument("--clear", action="store_true",
                   help="Clear the contact text (leaves the flag alone).")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    new_contact = None if args.clear else args.contact

    with RunLogger(engine, ws.name, STAGE) as run:
        with engine.begin() as conn:
            existing = conn.execute(
                select(
                    partners.c.partner_id,
                    partners.c.warm_path_available,
                    partners.c.warm_path_contact,
                ).where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[warm_contact] partner {args.partner_id!r} not found")
                run.failed = 1
                run.log_error(args.partner_id, "not_found", "no such partner")
                return 2
            conn.execute(
                partners.update()
                .where(partners.c.partner_id == args.partner_id)
                .values(warm_path_contact=new_contact, last_updated=_now())
            )
        msg = (
            f"{args.partner_id}: warm_path_contact "
            f"{existing.warm_path_contact!r} -> {new_contact!r} "
            f"(warm_path_available={existing.warm_path_available} unchanged)"
        )
        print(f"[warm_contact] {msg}")
        run.note(msg)
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
