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
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)
    # Batch 30 (#529): mode-aware refusal.
    if ws.mode == "fixture" and not args.allow_fixture_mode:
        print(
            f"[gmail_drafts] REFUSED: workspace mode=fixture; would draft "
            f"fictional partners. Pass --allow-fixture-mode to override."
        )
        return 2
    founder_email = (ws.company.get("company") or {}).get("founder_email")

    with RunLogger(engine, ws.name, STAGE) as run:
        try:
            gmail = GmailClient.from_workspace(ws)
        except GmailNotConfigured:
            msg = (
                f"Gmail not linked for workspace {ws.name!r}. "
                f"Run: uv run scripts/connect_gmail.py "
                f"--workspace {args.workspace or ws.path}"
            )
            print(f"[gmail_drafts] {msg}")
            run.failed = 1
            run.note("gmail_not_configured")
            return 2

        with engine.begin() as conn:
            rows = list(conn.execute(
                select(
                    partner_score_summaries.c.partner_id,
                    partner_score_summaries.c.send_now_priority,
                    partners.c.name.label("partner_name"),
                    partners.c.email,
                )
                .join(partners,
                      partners.c.partner_id == partner_score_summaries.c.partner_id)
                .where(partner_score_summaries.c.recommended_to_send.is_(True))
                .order_by(desc(partner_score_summaries.c.send_now_priority))
                .limit(args.top)
            ))

        if not rows:
            print("[gmail_drafts] no recommended_to_send partners; run Stage 6 + 7 first")
            run.skipped = 1
            return 0

        for row in rows:
            run.processed += 1
            if not row.email:
                run.skipped += 1
                print(
                    f"[gmail_drafts] skip {row.partner_id}: no email on file. "
                    f"Use scripts/set_partner_email.py to set one."
                )
                continue
            with engine.begin() as conn:
                rec = conn.execute(
                    select(email_drafts).where(
                        email_drafts.c.partner_id == row.partner_id,
                        email_drafts.c.is_recommended.is_(True),
                    ).order_by(desc(email_drafts.c.draft_id)).limit(1)
                ).first()
            if not rec:
                run.skipped += 1
                print(
                    f"[gmail_drafts] skip {row.partner_id}: no recommended "
                    f"email draft. Re-run Stage 7."
                )
                continue
            if rec.pushed_to_gmail_at and not args.regenerate:
                run.skipped += 1
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
                run.failed += 1
                msg = (
                    f"recommended draft has empty subject or body "
                    f"(subject={rec.subject!r}, "
                    f"body_len={len(rec.body or '')})"
                )
                run.log_error(row.partner_id, "empty_draft", msg)
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
            if args.allow_example_domains:
                prod_fails = [
                    f for f in prod_fails
                    if "example/reserved domain" not in f
                ]
            if prod_fails:
                run.failed += 1
                msg = "; ".join(prod_fails)
                run.log_error(row.partner_id, "prod_guard", msg)
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
                run.failed += 1
                run.log_error(row.partner_id, "GmailError", str(exc))
                print(f"[gmail_drafts] {row.partner_id}: FAILED -- {exc}")
                continue
            with engine.begin() as conn:
                conn.execute(
                    email_drafts.update()
                    .where(email_drafts.c.draft_id == rec.draft_id)
                    .values(pushed_to_gmail_at=_now(), gmail_draft_id=draft_id)
                )
            run.succeeded += 1
            print(f"[gmail_drafts] {row.partner_id} ({row.email}): {url}")

        print(
            f"[gmail_drafts] processed={run.processed} created={run.succeeded} "
            f"skipped={run.skipped} failed={run.failed}\n"
            f"Open https://mail.google.com/mail/u/0/#drafts to review and send."
        )
        return 2 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
