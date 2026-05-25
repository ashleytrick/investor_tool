"""Show all email_drafts versions for a partner (Slice 17).

Stage 7's re-run no longer hard-deletes prior drafts; it supersedes
them and inserts the new version. This CLI lets the operator see
the full history -- useful for "why did this draft change?" audits
or for picking a prior version to restore (manual SQL for now).

Examples:
  uv run scripts/list_draft_history.py --workspace clients/{name} \\
      --partner-id <pid>
  uv run scripts/list_draft_history.py --workspace clients/{name} \\
      --partner-id <pid> --json
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
from core.db import email_drafts, get_engine


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List versioned email_drafts history for a partner.",
    )
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_draft_history")

    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                email_drafts.c.draft_id,
                email_drafts.c.version,
                email_drafts.c.batch_id,
                email_drafts.c.strategy,
                email_drafts.c.is_recommended,
                email_drafts.c.approval_status,
                email_drafts.c.qa_status,
                email_drafts.c.generated_at,
                email_drafts.c.superseded_at,
                email_drafts.c.subject,
            )
            .where(email_drafts.c.partner_id == args.partner_id)
            .order_by(desc(email_drafts.c.version), desc(email_drafts.c.draft_id))
        ))

    if args.json:
        print(json.dumps([
            {
                "draft_id": r.draft_id,
                "version": r.version,
                "batch_id": r.batch_id,
                "strategy": r.strategy,
                "is_recommended": bool(r.is_recommended),
                "approval_status": r.approval_status,
                "qa_status": r.qa_status,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
                "subject": r.subject,
            }
            for r in rows
        ], indent=2))
        return 0

    if not rows:
        print(f"[draft_history] no drafts for partner_id={args.partner_id!r}")
        return 0

    print(f"== {len(rows)} draft(s) for partner_id={args.partner_id} ==")
    for r in rows:
        live = "LIVE   " if r.superseded_at is None else "SUPER  "
        rec = "REC " if r.is_recommended else "alt "
        print(
            f"  {live} v{r.version:>2} draft_id={r.draft_id:>5} {rec}"
            f"batch={r.batch_id!r:>14} approval={r.approval_status!r:<22} "
            f"qa={r.qa_status!r:<8} subject={r.subject!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
