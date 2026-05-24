"""Diff two Stage 7 batches: which partners were added/dropped/changed.

Used to answer "what changed between yesterday's draft pile and today's?
Did re-running Stage 4 / Stage 6 actually shift recommendations?"

Examples:
  uv run scripts/compare_batches.py --workspace clients/foo \\
      --before batch_20260301_120000 --after batch_20260302_120000
  uv run scripts/compare_batches.py --workspace clients/foo --json
  # (with no --before/--after, compares the two most recent batches)
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
from core.db import email_drafts, get_engine, partners


def _drafts_for_batch(engine, batch_id: str) -> dict[str, dict]:
    """partner_id -> {strategy, subject, body, qa_status, is_recommended}
    for the recommended row in `batch_id`."""
    out: dict[str, dict] = {}
    with engine.begin() as conn:
        for r in conn.execute(
            select(
                email_drafts.c.partner_id,
                email_drafts.c.strategy,
                email_drafts.c.subject,
                email_drafts.c.body,
                email_drafts.c.qa_status,
                partners.c.name.label("partner_name"),
            )
            .join(
                partners,
                partners.c.partner_id == email_drafts.c.partner_id,
            )
            .where(
                email_drafts.c.batch_id == batch_id,
                email_drafts.c.is_recommended.is_(True),
            )
        ):
            out[r.partner_id] = {
                "partner_name": r.partner_name,
                "strategy": r.strategy,
                "subject": r.subject,
                "body": r.body,
                "qa_status": r.qa_status,
            }
    return out


def _two_most_recent_batches(engine) -> tuple[str | None, str | None]:
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(email_drafts.c.batch_id)
            .distinct()
            .order_by(desc(email_drafts.c.batch_id))
            .limit(2)
        ))
    if len(rows) < 2:
        return (rows[0].batch_id if rows else None, None)
    return (rows[1].batch_id, rows[0].batch_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two Stage 7 batches.")
    add_workspace_arg(parser)
    parser.add_argument("--before", default=None,
                        help="Older batch_id (defaults to second-most-recent).")
    parser.add_argument("--after", default=None,
                        help="Newer batch_id (defaults to most-recent).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="compare_batches")

    before = args.before
    after = args.after
    if not before or not after:
        b, a = _two_most_recent_batches(engine)
        before = before or b
        after = after or a
    if not before or not after:
        print("[compare] need at least two batches to compare")
        return 2

    before_map = _drafts_for_batch(engine, before)
    after_map = _drafts_for_batch(engine, after)

    added = sorted(set(after_map) - set(before_map))
    dropped = sorted(set(before_map) - set(after_map))
    common = sorted(set(before_map) & set(after_map))

    strategy_changed: list[dict] = []
    subject_changed: list[dict] = []
    qa_changed: list[dict] = []
    for pid in common:
        b, a = before_map[pid], after_map[pid]
        if b["strategy"] != a["strategy"]:
            strategy_changed.append({
                "partner_id": pid, "name": a["partner_name"],
                "before": b["strategy"], "after": a["strategy"],
            })
        if b["subject"] != a["subject"]:
            subject_changed.append({
                "partner_id": pid, "name": a["partner_name"],
                "before": b["subject"], "after": a["subject"],
            })
        if b["qa_status"] != a["qa_status"]:
            qa_changed.append({
                "partner_id": pid, "name": a["partner_name"],
                "before": b["qa_status"], "after": a["qa_status"],
            })

    if args.json:
        print(json.dumps({
            "before": before, "after": after,
            "added": [{"partner_id": p, "name": after_map[p]["partner_name"]}
                      for p in added],
            "dropped": [{"partner_id": p, "name": before_map[p]["partner_name"]}
                        for p in dropped],
            "strategy_changed": strategy_changed,
            "subject_changed": subject_changed,
            "qa_changed": qa_changed,
        }, indent=2))
        return 0

    print()
    print(f"== Comparing {before!r} -> {after!r} ==")
    print(f"  added ({len(added)}):")
    for p in added:
        print(f"    + {p} ({after_map[p]['partner_name']})")
    print(f"  dropped ({len(dropped)}):")
    for p in dropped:
        print(f"    - {p} ({before_map[p]['partner_name']})")
    if strategy_changed:
        print(f"  strategy changed ({len(strategy_changed)}):")
        for c in strategy_changed:
            print(
                f"    ~ {c['partner_id']}: {c['before']} -> {c['after']}"
            )
    if subject_changed:
        print(f"  subject changed ({len(subject_changed)}):")
        for c in subject_changed:
            print(f"    ~ {c['partner_id']}")
    if qa_changed:
        print(f"  qa_status changed ({len(qa_changed)}):")
        for c in qa_changed:
            print(
                f"    ~ {c['partner_id']}: {c['before']} -> {c['after']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
