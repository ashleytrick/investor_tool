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

from core.approval.gate import can_approve_draft, split_blockers
from core.approval.persistence import approve
from core.config_loader import add_workspace_arg
from core.db import email_drafts
from core.deliverability import (
    configured_daily_cap, enforce_daily_approval_cap,
)
from core.operator_command import operator_command_run

STAGE = "approve_draft"


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
    parser.add_argument(
        "--override-cap", action="store_true",
        help="Approve even when today's daily approval cap is reached. "
             "Use sparingly; the cap exists to prevent runaway sends.",
    )
    parser.add_argument(
        "--override-blockers", action="store_true",
        help="Approve even when the approval gate reports blockers "
             "(missing email, DNC, suppression, qa_status=fail, etc.). "
             "Requires --notes -- the operator's rationale is recorded "
             "with the approval event.",
    )
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit .example fixture data through the approval gate. "
             "Only useful for fixture smoke tests.",
    )
    args = parser.parse_args()
    actor = _resolve_actor(args.approved_by)

    with operator_command_run(args, stage=STAGE) as ctx:
        ws, engine, run = ctx.ws, ctx.engine, ctx.run

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
            ctx.usage_error(f"draft_id={args.draft_id} not found")
            return ctx.exit_code

        # Re-derive the approval gate from LIVE DB state. Stage 7's
        # eagerly-computed blockers can't be trusted at this point --
        # state moves between draft generation and approval (partner
        # email gets set, DNC flag flipped, relationship transitions
        # to active_conversation). can_approve_draft consults the
        # canonical current values and re-runs the same rules.
        gate = can_approve_draft(
            ws, engine, args.draft_id,
            allow_example_domains=args.allow_example_domains,
        )
        soft_overrides: list[str] = []
        if not gate.ok:
            hard, soft = split_blockers(gate.blockers)
            if not args.override_blockers:
                print(
                    f"[approve] REFUSED: {len(gate.blockers)} blocker(s) "
                    f"prevent approval:"
                )
                for b in gate.blockers:
                    print(f"  - {b}")
                print(
                    "[approve] resolve the blockers (Apollo upload, "
                    "clear DNC, etc.) or pass --override-blockers "
                    "--notes '<rationale>' to approve anyway."
                )
                ctx.refuse(
                    f"approval blockers: {'; '.join(gate.blockers)}",
                )
                return ctx.exit_code
            # PR #7 follow-up review: hard blockers (missing email,
            # do-not-contact, invalid verification, etc.) can never be
            # overridden. The operator must fix the underlying state.
            if hard:
                print(
                    f"[approve] REFUSED: {len(hard)} HARD blocker(s) "
                    f"cannot be bypassed by --override-blockers:"
                )
                for b in hard:
                    print(f"  - {b}")
                print(
                    "[approve] resolve these (set partner email, clear "
                    "DNC, fix verification) before re-attempting approval."
                )
                ctx.refuse(
                    f"hard approval blockers: {'; '.join(hard)}",
                )
                return ctx.exit_code
            if not (args.notes or "").strip():
                print(
                    "[approve] REFUSED: --override-blockers requires "
                    "--notes explaining the operator's rationale "
                    "(recorded on the approval event for audit)."
                )
                ctx.refuse("override without --notes")
                return ctx.exit_code
            soft_overrides = list(soft)
            print(
                f"[approve] OVERRIDE: {len(soft_overrides)} soft "
                f"blocker(s) acknowledged via --override-blockers:"
            )
            for b in soft_overrides:
                print(f"  - {b}")

        # Slice 9: daily approval cap. Block when reached unless the
        # operator explicitly overrides. Finding 6: cap is configurable
        # via company.yaml's `deliverability.daily_approval_cap`.
        cap = configured_daily_cap(ws)
        blocked, count = enforce_daily_approval_cap(engine, cap=cap)
        if blocked and not args.override_cap:
            print(
                f"[approve] REFUSED: daily approval cap reached "
                f"({count} approved today / cap {cap}). Pass "
                f"--override-cap to approve anyway."
            )
            ctx.refuse(f"daily approval cap reached ({count})")
            return ctx.exit_code

        # Persist the soft overrides structurally on the approval event
        # so downstream gate re-checks (Gmail / Attio / send-queue) can
        # honor them. The notes field still gets a human-readable
        # summary prefix for the audit log.
        approval_notes = args.notes
        if soft_overrides:
            prefix = f"[OVERRODE BLOCKERS: {'; '.join(soft_overrides)}] "
            approval_notes = prefix + (approval_notes or "")

        try:
            approve(
                engine,
                draft_id=args.draft_id,
                partner_id=row.partner_id,
                actor=actor,
                notes=approval_notes,
                overridden_blockers=soft_overrides or None,
            )
        except Exception as exc:  # noqa: BLE001 - surface to operator
            print(f"[approve] REFUSED: {exc}")
            ctx.refuse(f"approve raised: {exc}")
            return ctx.exit_code

        print(
            f"[approve] draft_id={args.draft_id} "
            f"partner={row.partner_id} subject={row.subject!r} -> "
            f"approved_to_send by {actor!r}"
        )
        run.note(
            f"approved draft_id={args.draft_id} partner={row.partner_id} "
            f"by {actor}"
        )
        run.succeeded = 1
        run.processed = 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
