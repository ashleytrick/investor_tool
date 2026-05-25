"""Human rejection CLI.

Moves a draft from needs_review (or approved_to_send) to rejected.
Use when the draft is off-base and shouldn't go to send queue. A
rejected draft can be un-rejected later (rejected -> needs_review)
if the operator changes their mind.

Usage:
  uv run scripts/reject_draft.py --workspace clients/{name} \\
      --draft-id 42 \\
      --reason "wrong fund stage focus; partner not relevant"
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.approval.persistence import reject
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import email_drafts, get_engine


def _resolve_actor(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    return (
        os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "unknown"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject a draft (won't reach the send queue).",
    )
    add_workspace_arg(parser)
    parser.add_argument("--draft-id", type=int, required=True)
    parser.add_argument(
        "--reason", required=True,
        help="Human-readable rejection reason (mandatory for audit).",
    )
    parser.add_argument(
        "--rejected-by", default=None,
        help="Override the operator id.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="reject_draft")
    actor = _resolve_actor(args.rejected_by)

    with engine.begin() as conn:
        row = conn.execute(
            select(
                email_drafts.c.partner_id,
                email_drafts.c.approval_status,
            ).where(email_drafts.c.draft_id == args.draft_id)
        ).first()
    if row is None:
        print(f"[reject] draft_id={args.draft_id} not found")
        return 1

    try:
        reject(
            engine,
            draft_id=args.draft_id,
            partner_id=row.partner_id,
            actor=actor,
            notes=args.reason,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[reject] REFUSED: {exc}")
        return 2

    print(
        f"[reject] draft_id={args.draft_id} partner={row.partner_id} "
        f"-> rejected by {actor!r}: {args.reason!r}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
