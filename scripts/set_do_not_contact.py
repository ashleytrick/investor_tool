"""Mark a partner as do_not_contact (#441/#684).

The flag is consulted by Stage 6 (as a major_kill component) and
Stage 7 (routes outreach_status='do_not_contact'). Distinct from
warm-path: warm-path means "use the warm channel"; do_not_contact
means "use NO channel".

Slice 15 audit metadata: every DNC set/clear writes set_at, set_by,
and source so the operator can trace why a partner was suppressed
months later. `--source` defaults to "manual" (this CLI); the
outcome-sync adapter passes "attio" when a CRM event hydrates the
flag, the reply classifier passes "gmail", a CSV importer passes
"csv".

Examples:
  uv run scripts/set_do_not_contact.py --partner-id NAME \\
      --reason "conflict of interest with existing investor"
  uv run scripts/set_do_not_contact.py --partner-id NAME --clear
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.approval.persistence import stale_live_approvals_for_partner
from core.approval.state_machine import TRIGGER_DO_NOT_CONTACT_SET
from core.config_loader import add_workspace_arg
from core.db import partners
from core.operator_command import operator_command_run

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
    parser.add_argument(
        "--source", default="manual",
        choices=("manual", "attio", "gmail", "csv"),
        help="Where the DNC decision came from. Used by audits to "
             "distinguish operator action from automated hydration.",
    )
    parser.add_argument(
        "--set-by", default=None,
        help="Operator identifier (defaults to $USER / $USERNAME). "
             "Recorded as do_not_contact_set_by on the partner row.",
    )
    args = parser.parse_args()

    set_flag = not args.clear
    actor = (
        args.set_by
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown"
    )

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            existing = conn.execute(
                select(
                    partners.c.partner_id, partners.c.name,
                    partners.c.do_not_contact,
                ).where(partners.c.partner_id == args.partner_id)
            ).first()
            if not existing:
                print(f"[dnc] partner {args.partner_id!r} not found")
                ctx.refuse("no such partner")
                run.log_error(args.partner_id, "not_found", "no such partner")
                return ctx.exit_code
            old = bool(existing.do_not_contact)
            now = _now()
            conn.execute(
                partners.update()
                .where(partners.c.partner_id == args.partner_id)
                .values(
                    do_not_contact=set_flag,
                    do_not_contact_reason=args.reason if set_flag else None,
                    # Slice 15: audit metadata. On --clear we wipe set_at /
                    # set_by / source too so a future audit can tell
                    # "currently clear, no prior DNC" from "currently set,
                    # set by X at Y".
                    do_not_contact_set_at=now if set_flag else None,
                    do_not_contact_set_by=actor if set_flag else None,
                    do_not_contact_source=args.source if set_flag else None,
                    last_updated=now,
                )
            )
        msg = (
            f"{args.partner_id} ({existing.name}): do_not_contact "
            f"{old} -> {set_flag} ({(args.reason or '-')!r}) "
            f"by {actor} via {args.source}"
        )
        print(f"[dnc] {msg}")
        run.note(msg)
        # Setting DNC on a partner must invalidate any live approved
        # drafts: we promised not to send cold to this person, period.
        # Only fires on a 0->1 transition; clearing DNC is fine to
        # leave alone (operator can re-approve manually).
        if set_flag and not old:
            staled = stale_live_approvals_for_partner(
                engine,
                partner_id=args.partner_id,
                trigger=TRIGGER_DO_NOT_CONTACT_SET,
                notes=args.reason or None,
            )
            if staled:
                print(
                    f"[dnc] staled {staled} approved draft(s) for "
                    f"{args.partner_id} (DNC just set)"
                )
        run.succeeded = 1
        run.processed = 1
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
