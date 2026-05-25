"""Manually set a partner's relationship_status (Slice 7).

The outcome-sync hydration handles most updates automatically (Attio
events -> relationship state). This CLI exists for cases the
automation can't see: a quick "I just spoke with X at an event"
update, a manual `passed` recording before outcome sync catches it,
explicit `do_not_contact`, etc.

Slice 7 invalidation: if the partner has approved drafts and the
new relationship state would suppress outreach, those approvals are
flipped to stale_after_approval automatically (state-machine
invalidation rule: relationship_changed).

Usage:
  uv run scripts/set_relationship.py --workspace clients/{name} \\
      --partner-id alice.example_jane_doe \\
      --status active_conversation --notes "met at fintech panel"
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update

from core.approval.persistence import mark_stale
from core.approval.state_machine import (
    STATE_APPROVED_TO_SEND, TRIGGER_RELATIONSHIP_CHANGED,
)
from core.config_loader import add_workspace_arg
from core.db import email_drafts, partners
from core.operator_command import operator_command_run
from core.relationships import (
    ALL_STATES, suppress_outreach,
)

STAGE = "set_relationship"


def _actor(cli: str | None) -> str:
    return cli or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set a partner's relationship_status manually.",
    )
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    parser.add_argument(
        "--status", required=True, choices=sorted(ALL_STATES),
    )
    parser.add_argument(
        "--notes", default=None,
        help="Owner notes (free text). Appended to partners.owner_notes.",
    )
    parser.add_argument("--actor", default=None)
    args = parser.parse_args()
    actor = _actor(args.actor)

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        with engine.begin() as conn:
            row = conn.execute(
                select(
                    partners.c.relationship_status,
                    partners.c.do_not_contact,
                    partners.c.owner_notes,
                ).where(partners.c.partner_id == args.partner_id),
            ).first()
            if row is None:
                print(
                    f"[set_relationship] partner_id={args.partner_id!r} "
                    f"not found"
                )
                ctx.usage_error(
                    f"partner_id={args.partner_id!r} not found"
                )
                return ctx.exit_code
            existing_notes = row.owner_notes or ""
            new_notes = (
                f"{existing_notes}\n{datetime.now(timezone.utc).isoformat()} "
                f"({actor}): {args.notes}"
            ).strip() if args.notes else existing_notes
            conn.execute(
                update(partners)
                .where(partners.c.partner_id == args.partner_id)
                .values(
                    relationship_status=args.status,
                    owner_notes=new_notes,
                    relationship_updated_at=datetime.now(timezone.utc),
                    outcome_source="manual",
                    last_outcome=args.status,
                )
            )

        # If the new status would suppress outreach, stale any approved
        # drafts for this partner (state-machine invalidation rule:
        # relationship_changed).
        suppression = suppress_outreach(
            relationship_status=args.status,
            last_contacted_at=None,
            last_reply_at=None,
            do_not_contact=row.do_not_contact or False,
        )
        if suppression.suppressed:
            with engine.begin() as conn:
                approved = list(conn.execute(
                    select(email_drafts.c.draft_id).where(
                        email_drafts.c.partner_id == args.partner_id,
                        email_drafts.c.approval_status == STATE_APPROVED_TO_SEND,
                    )
                ))
            for d in approved:
                mark_stale(
                    engine, draft_id=int(d.draft_id),
                    partner_id=args.partner_id,
                    trigger=TRIGGER_RELATIONSHIP_CHANGED,
                    notes=(
                        f"relationship_status={args.status}: "
                        f"{suppression.reason}"
                    ),
                )
            if approved:
                print(
                    f"[set_relationship] {args.partner_id} -> "
                    f"{args.status}; marked {len(approved)} approved "
                    f"draft(s) stale_after_approval "
                    f"(suppression: {suppression.reason})"
                )
                run.note(
                    f"set {args.partner_id}->{args.status} by {actor}; "
                    f"staled {len(approved)} approval(s)"
                )
                run.processed = 1
                run.succeeded = 1
                return 0

        print(
            f"[set_relationship] {args.partner_id} -> {args.status} "
            f"by {actor}"
        )
        run.note(f"set {args.partner_id}->{args.status} by {actor}")
        run.processed = 1
        run.succeeded = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
