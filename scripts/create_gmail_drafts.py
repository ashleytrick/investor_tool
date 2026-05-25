"""For each recommended partner with a known email, create a Gmail DRAFT.

The operator opens Gmail Drafts, reviews, hits send. Drafts only; this script
never sends. Idempotent: a partner whose recommended draft already has
pushed_to_gmail_at set is skipped (re-run after a Stage 7 regeneration to
create fresh drafts).

Setup:
  1. GCP project + Gmail API enabled.
  2. OAuth Desktop-app client credentials JSON saved to
     clients/{workspace}/.gmail_credentials.json.
  3. First run opens a browser for consent (one-time).

Run:
  uv run scripts/create_gmail_drafts.py
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import email_drafts, get_engine, partner_score_summaries, partners
from core.gmail_client import GmailClient, GmailError, GmailNotConfigured
from core.production_guards import production_gate_for_gmail_draft
from core.runs import RunLogger

STAGE = "create_gmail_drafts"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Gmail drafts for recommended partners.")
    add_workspace_arg(parser)
    parser.add_argument("--top", type=int, default=25,
                        help="Only create drafts for top-N partners by send_now_priority.")
    parser.add_argument("--regenerate", action="store_true",
                        help="Recreate drafts even if pushed_to_gmail_at is already set.")
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit RFC 2606 reserved domains (.example/.test/.invalid) "
             "in recipient/sender email. Use for fixture testing ONLY; "
             "production runs should refuse so fictional partners cannot "
             "be drafted in Gmail.",
    )
    # Batch 30 (#529): mode-aware refusal.
    parser.add_argument(
        "--allow-fixture-mode", action="store_true",
        help="Bypass the mode=fixture refusal. Required when company.yaml "
             "has `mode: fixture` -- prevents accidental Gmail drafts of "
             "fictional partners.",
    )
    # Mirror Stage 8's --require-attio: when an operator depends on
    # Gmail drafting as part of production delivery, missing Gmail
    # config should be a HARD failure, not a quiet skip. ws.mode ==
    # "prod" also implies require-gmail so a prod cron can't quietly
    # skip the draft step without the operator noticing.
    parser.add_argument(
        "--require-gmail", action="store_true",
        help="Refuse to skip when Gmail isn't linked for the workspace. "
             "Use in production cron entries that depend on draft "
             "creation; missing creds become a fail instead of a skip.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    # WorkspacePolicy centralizes the prod-mode-implies-strict /
    # fixture-mode-refuses-real-writes derivations (Refactor item 10).
    from core.workspace_policy import WorkspacePolicy
    policy = WorkspacePolicy.from_workspace_and_args(ws, args)
    # Batch 30 (#529): mode-aware refusal.
    if policy.refuses_fixture_data():
        print(
            f"[gmail_drafts] REFUSED: workspace mode=fixture; would draft "
            f"fictional partners. Pass --allow-fixture-mode to override."
        )
        return 2
    founder_email = (ws.company.get("company") or {}).get("founder_email")

    # Batch 35: open RunLogger BEFORE the GmailNotConfigured branch so
    # the "Gmail not linked, skipping" outcome lands in `runs` with
    # records_skipped=1 instead of writing no run row at all. status.py
    # + the operator can then see when this stage was last attempted.
    require_gmail = policy.require_gmail
    try:
        gmail = GmailClient.from_workspace(ws)
    except GmailNotConfigured:
        with RunLogger(engine, ws.name, STAGE) as run:
            msg = (
                f"Gmail not linked for workspace {ws.name!r}. Run: "
                f"uv run scripts/connect_gmail.py "
                f"--workspace {args.workspace or ws.path}"
            )
            if require_gmail:
                # Prod-mode (or explicit --require-gmail): missing creds
                # is a fail, not a skip, so cron / wrappers notice.
                print(f"[gmail_drafts] REFUSED: {msg}")
                run.note(f"REFUSED: {msg}")
                run.failed = 1
            else:
                print(f"[gmail_drafts] {msg}")
                run.note(msg)
                run.skipped = 1
        return 2 if require_gmail else 0

    with RunLogger(engine, ws.name, STAGE) as run:
        # Slice 1: Gmail send queue reads ONLY drafts in
        # approval_status='approved_to_send'. Recommended_to_send is
        # no longer sufficient -- a human must have explicitly
        # approved the exact body. core.approval.persistence.approved_for_send
        # is the single canonical read.
        from core.approval.persistence import approved_for_send

        approved_drafts = approved_for_send(engine)
        if not approved_drafts:
            print(
                "[gmail_drafts] no approved_to_send drafts. "
                "Run Stage 7 to generate, then "
                "scripts/list_pending_review.py / approve_draft.py "
                "to approve."
            )
            run.skipped = 1
            return 0

        # Join the approved drafts to partner email + name. Done in
        # Python to keep the canonical approval read intact.
        with engine.begin() as conn:
            partner_rows = {
                r.partner_id: r for r in conn.execute(
                    select(
                        partners.c.partner_id,
                        partners.c.name.label("partner_name"),
                        partners.c.email,
                    )
                )
            }

        # Apply --top by partner send_now_priority. Multiple approved
        # drafts per partner go in priority order; ties broken by
        # draft_id ascending so the earliest approval wins.
        with engine.begin() as conn:
            priority_by_partner = {
                r.partner_id: r.send_now_priority for r in conn.execute(
                    select(
                        partner_score_summaries.c.partner_id,
                        partner_score_summaries.c.send_now_priority,
                    )
                )
            }
        # Sort approved drafts: priority DESC then draft_id ASC.
        approved_drafts = sorted(
            approved_drafts,
            key=lambda d: (
                -(priority_by_partner.get(d.partner_id) or 0.0),
                d.draft_id,
            ),
        )[: args.top]

        for rec in approved_drafts:
            with run.attempt():
                partner = partner_rows.get(rec.partner_id)
                if partner is None:
                    run.fail(
                        rec.partner_id, "orphan_approval",
                        "approved draft references unknown partner",
                    )
                    continue
                if not partner.email:
                    # An approved draft should always have an email
                    # (Slice 1's approval blocker prevents otherwise).
                    # Defense in depth: surface as fail, not silent skip.
                    run.fail(
                        rec.partner_id, "missing_email_post_approval",
                        f"draft_id={rec.draft_id} approved but partner "
                        f"email is missing -- approval should be stale",
                    )
                    continue
                if rec.pushed_to_gmail_at and not args.regenerate:
                    run.skip()
                    print(
                        f"[gmail_drafts] skip draft_id={rec.draft_id}"
                        f" partner={rec.partner_id}: already pushed at"
                        f" {rec.pushed_to_gmail_at}. Use --regenerate to "
                        f"force."
                    )
                    continue
                # Keep `row` shape for the rest of the body which
                # was written against the old (partner-row, draft-row)
                # pair.
                row = partner
                if rec.pushed_to_gmail_at and not args.regenerate:
                    run.skip()
                    print(
                        f"[gmail_drafts] skip {row.partner_id}: already pushed "
                        f"at {rec.pushed_to_gmail_at}. Use --regenerate to force."
                    )
                    continue
                # Refuse to push an empty draft. The schema permits NULL on body
                # / subject (no NOT NULL on email_drafts) and Gmail's API would
                # reject the request with a cryptic "Invalid request" -- catch
                # it here so the operator sees which partner is bad.
                if not (rec.subject or "").strip() or not (rec.body or "").strip():
                    msg = (
                        f"recommended draft has empty subject or body "
                        f"(subject={rec.subject!r}, "
                        f"body_len={len(rec.body or '')})"
                    )
                    run.fail(row.partner_id, "empty_draft", msg)
                    print(f"[gmail_drafts] {row.partner_id}: FAILED -- {msg}")
                    continue
                # Batch 9 production guard: refuse to push to a fictional
                # recipient (.example/.test/.invalid) or with a placeholder
                # subject/body left over from an unedited workspace.
                prod_fails = production_gate_for_gmail_draft(
                    to_email=row.email,
                    from_email=founder_email,
                    subject=rec.subject,
                    body=rec.body,
                )
                # Filter out .example checks if the operator opted in.
                if policy.allow_example_domains:
                    prod_fails = [
                        f for f in prod_fails
                        if "example/reserved domain" not in f
                    ]
                if prod_fails:
                    msg = "; ".join(prod_fails)
                    run.fail(row.partner_id, "prod_guard", msg)
                    print(
                        f"[gmail_drafts] {row.partner_id}: PROD GUARD -- {msg} "
                        f"(pass --allow-example-domains for fixture testing)"
                    )
                    continue
                try:
                    draft_id, url = gmail.create_draft(
                        to_email=row.email,
                        subject=rec.subject,
                        body=rec.body,
                        from_email=founder_email,
                    )
                except GmailError as exc:
                    run.fail(row.partner_id, "GmailError", str(exc))
                    print(f"[gmail_drafts] {row.partner_id}: FAILED -- {exc}")
                    continue
                with engine.begin() as conn:
                    conn.execute(
                        email_drafts.update()
                        .where(email_drafts.c.draft_id == rec.draft_id)
                        .values(pushed_to_gmail_at=_now(), gmail_draft_id=draft_id)
                    )
                print(f"[gmail_drafts] {row.partner_id} ({row.email}): {url}")

        print(
            f"[gmail_drafts] processed={run.processed} created={run.succeeded} "
            f"skipped={run.skipped} failed={run.failed}\n"
            f"Open https://mail.google.com/mail/u/0/#drafts to review and send."
        )
        # Batch 35: non-zero exit if any per-partner draft failed so cron
        # / wrapping scripts notice partial Gmail draft failures.
        any_failed = run.failed > 0

    return 2 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
