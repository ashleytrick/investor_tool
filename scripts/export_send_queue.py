"""Write clients/{name}/exports/send_queue.csv with ONLY approved drafts.

The review queue (Stage 7) lists every draft regardless of approval
state. THIS export is the small, filtered view: one row per draft in
approval_status='approved_to_send' with the recipient + body the
operator (or downstream sender) is cleared to actually send.

Includes:
  - draft_id (so sends can be reconciled back to the DB)
  - draft_hash (proves the body matches what was approved)
  - approved_at + approved_by (latest approval event)
  - subject + body + followup + deck
  - partner_email (the recipient)
  - partner_name, fund_name (for the operator's eyes)

Slice 1: this is the ONLY CSV any "send this" workflow should read.
Stage 8 sync and Gmail draft creation both use the same gate via
core.approval.persistence.approved_for_send().

Usage:
  uv run scripts/export_send_queue.py --workspace clients/{name}
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select, desc

from core.approval.gate import can_approve_draft
from core.approval.persistence import approved_for_send
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.csv_export import write_send_queue
from core.db import (
    deck_request_responses,
    draft_approvals,
    followup_drafts,
    get_engine,
    partners,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export approved drafts as send_queue.csv.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit .example fixture data through the export gate.",
    )
    parser.add_argument(
        "--skip-stale-approvals", action="store_true",
        help="Drop approved drafts that now have a fresh blocker "
             "(missing email, DNC flipped on, verification regressed) "
             "instead of refusing the export. Default behavior is to "
             "REFUSE so the operator notices and re-approves.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="export_send_queue")

    drafts = approved_for_send(engine)
    if not drafts:
        print(
            "[send_queue] no approved_to_send drafts. "
            "Run scripts/list_pending_review.py to see what's waiting "
            "for approval."
        )
        # Still write an empty file so downstream consumers don't
        # see a stale send_queue.csv from a prior run.
        out_path = write_send_queue(ws.exports_dir, [])
        print(f"[send_queue] wrote empty CSV -> {out_path}")
        return 0

    with engine.begin() as conn:
        partner_by_id = {
            r.partner_id: r for r in conn.execute(
                select(
                    partners.c.partner_id,
                    partners.c.name.label("partner_name"),
                    partners.c.email.label("partner_email"),
                    partners.c.fund_id,
                )
            )
        }
        # Followup + deck per partner (latest by id ascending wins).
        followup_by_partner = {
            f.partner_id: f.body for f in conn.execute(
                select(followup_drafts).order_by(
                    followup_drafts.c.followup_id.asc(),
                )
            )
        }
        deck_by_partner = {
            d.partner_id: d.body for d in conn.execute(
                select(deck_request_responses).order_by(
                    deck_request_responses.c.response_id.asc(),
                )
            )
        }
        # Latest approved_to_send event per draft for actor + at.
        approval_by_draft: dict[int, tuple[str | None, datetime | None]] = {}
        for ev in conn.execute(
            select(draft_approvals).where(
                draft_approvals.c.event_type == "approved_to_send",
            ).order_by(desc(draft_approvals.c.event_id))
        ):
            approval_by_draft.setdefault(
                ev.draft_id, (ev.actor, ev.at),
            )
        # Fund name lookup via the funds table.
        from core.db import funds
        fund_name_by_id = {
            f.fund_id: f.name for f in conn.execute(select(funds))
        }

    rows: list[dict] = []
    # Finding 5: re-check the approval gate at export time. An
    # approved_to_send pointer set hours ago can be stale (partner
    # email cleared, DNC flipped on, verification regressed) -- the
    # send queue must NOT carry a row that fails the live gate.
    stale: list[tuple[int, tuple[str, ...]]] = []
    for d in drafts:
        partner = partner_by_id.get(d.partner_id)
        if partner is None:
            # An approved draft for a deleted partner. Drop + log.
            print(
                f"[send_queue] WARN: draft_id={d.draft_id} approved "
                f"but partner_id={d.partner_id!r} not found; skipping"
            )
            continue
        gate = can_approve_draft(
            ws, engine, d.draft_id,
            allow_example_domains=args.allow_example_domains,
        )
        if not gate.ok:
            stale.append((d.draft_id, gate.blockers))
            continue
        approval = approval_by_draft.get(d.draft_id, (None, None))
        rows.append({
            "draft_id": d.draft_id,
            "partner_id": d.partner_id,
            "partner_name": partner.partner_name or "",
            "partner_email": partner.partner_email or "",
            "fund_name": fund_name_by_id.get(partner.fund_id, "") or "",
            "approved_at": approval[1].isoformat() if approval[1] else "",
            "approved_by": approval[0] or "",
            "email_subject_line": d.subject or "",
            "outreach_email_draft": d.body or "",
            "followup_email_draft": followup_by_partner.get(d.partner_id, "") or "",
            "deck_request_response": deck_by_partner.get(d.partner_id, "") or "",
            "draft_hash": d.draft_hash or "",
        })

    if stale:
        print(
            f"[send_queue] {len(stale)} approved draft(s) now fail the "
            f"approval gate (stale-after-approval):"
        )
        for did, blockers in stale:
            print(f"  draft_id={did} -> {'; '.join(blockers)}")
        if not args.skip_stale_approvals:
            print(
                "[send_queue] REFUSED: at least one approved draft has "
                "a fresh blocker. Re-approve the affected drafts (or "
                "pass --skip-stale-approvals to export only the still-"
                "clean rows)."
            )
            return 2

    out_path = write_send_queue(ws.exports_dir, rows)
    print(
        f"[send_queue] {len(rows)} approved draft(s) -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
