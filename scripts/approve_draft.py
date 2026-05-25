"""Human approval CLI -- the ONLY path into `approved_to_send`.

Validates the transition against the state machine, writes the event
to draft_approvals, and updates the email_drafts.approval_status
pointer + draft_hash IN ONE TRANSACTION. Subsequent regenerations
that change the body produce a different draft_hash and trigger
`stale_after_approval` automatically (see Stage 7's regeneration
path).

Usage:
  uv run scripts/approve_draft.py --workspace clients/{name} \\
      --draft-id 42 \\
      --notes "wedge framing matches the partner's recent post"

The CLI looks up the partner_id from the draft so the operator only
needs the draft_id. --approved-by defaults to $USER / $USERNAME; pass
it explicitly when running under cron / shared accounts.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.approval.persistence import approve
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import email_drafts, get_engine


def _resolve_actor(cli_value: str | None) -> str:
    """Pick the operator id: explicit flag -> $USER -> $USERNAME ->
    'unknown'. Future single-user UI passes the cookie identity here."""
    if cli_value:
        return cli_value
    return (
        os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Approve a draft for cold-outreach send.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--draft-id", type=int, required=True,
        help="email_drafts.draft_id of the draft to approve.",
    )
    parser.add_argument(
        "--notes", default=None,
        help="Operator notes -- why this draft is good to send.",
    )
    parser.add_argument(
        "--approved-by", default=None,
        help="Override the operator id (defaults to $USER / $USERNAME).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="approve_draft")
    actor = _resolve_actor(args.approved_by)

    with engine.begin() as conn:
        row = conn.execute(
            select(
                email_drafts.c.partner_id,
                email_drafts.c.approval_status,
                email_drafts.c.subject,
            ).where(email_drafts.c.draft_id == args.draft_id)
        ).first()
    if row is None:
        print(f"[approve] draft_id={args.draft_id} not found")
        return 1

    try:
        approve(
            engine,
            draft_id=args.draft_id,
            partner_id=row.partner_id,
            actor=actor,
            notes=args.notes,
        )
    except Exception as exc:  # noqa: BLE001 - surface to operator
        print(f"[approve] REFUSED: {exc}")
        return 2

    print(
        f"[approve] draft_id={args.draft_id} "
        f"partner={row.partner_id} subject={row.subject!r} -> "
        f"approved_to_send by {actor!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
