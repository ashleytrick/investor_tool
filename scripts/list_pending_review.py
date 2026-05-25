"""List draft approvals waiting for operator review.

Drafts in `needs_review` or `stale_after_approval` are surfaced here.
Other states (approved_to_send / rejected / sent) are NOT shown -- they
either belong in the send queue or the operator has already decided.

Usage:
  uv run scripts/list_pending_review.py --workspace clients/{name}

Slice 1: this is the operator's primary read entry-point into the
approval workflow. A future UI / web view will hit the same
core.approval.persistence.pending_review() function so the data shape
stays uniform.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.approval.persistence import (
    list_events, pending_review,
)
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List drafts awaiting human approval.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--verbose", action="store_true",
        help="Also print each draft's full event history.",
    )
    parser.add_argument(
        "--show-body", action="store_true",
        help="Include the full draft body (long output).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="list_pending_review")

    rows = pending_review(engine)
    if not rows:
        print("[review] no drafts waiting for review")
        return 0

    print(
        f"[review] {len(rows)} draft(s) pending. "
        f"Use scripts/approve_draft.py / reject_draft.py to act."
    )
    for r in rows:
        print(
            f"\n  draft_id={r.draft_id}  partner={r.partner_id}  "
            f"status={r.approval_status}  strategy={r.strategy}"
        )
        print(f"    subject: {r.subject}")
        if args.show_body:
            for line in (r.body or "").splitlines():
                print(f"      {line}")
        if args.verbose:
            events = list_events(engine, r.draft_id)
            print("    history:")
            for e in events:
                actor = e.actor or "?"
                notes = f" ({e.notes})" if e.notes else ""
                print(f"      {e.at} {e.event_type} by {actor}{notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
