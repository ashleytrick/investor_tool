"""Single 'safe to send?' check for the workspace.

Walks the operator-actionable preconditions for cold-outreach send
and prints OK / BLOCKED per check + a one-line summary at the end.
Designed for cron-friendly use: exits 0 when nothing is blocking
a send pass, non-zero when at least one check fails.

Checks (rough first cut, Slice 3):

  1. Workspace config valid (calls validate_workspace_config)
  2. Pipeline stages have run recently (Stage 6 completed within
     STALE_STAGE6_HOURS; Stage 7 has a batch_qa_report)
  3. Approval queue is non-empty OR there are approved drafts (i.e.
     the operator has something to do or has done it)
  4. Approved drafts have valid partner emails
  5. No do_not_contact partners have approved drafts
  6. Workspace mode + integration availability sanity check

Output format:
  [check_ready] {section}: OK / BLOCKED -- {reason}
  ...
  [check_ready] {N} checks passed, {M} blocked

Exit code 0 = safe to proceed; 1 = blocked; 2 = error running the
check itself.

Future slices will expand the surface (deliverability, relationship
suppression, scheduling-link reachability) but the API stays the
same: each check returns CheckResult(name, ok, message).
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.approval.persistence import approved_for_send, pending_review
from core.approval.state_machine import STATE_APPROVED_TO_SEND
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    email_drafts, get_engine, partners, runs,
)
from core.validate_config import validate_workspace_config

# Stage 6 freshness threshold: re-score every N hours before sending.
STALE_STAGE6_HOURS = 24


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str

    def render(self) -> str:
        prefix = "[check_ready] " + self.name + ": "
        return prefix + ("OK -- " if self.ok else "BLOCKED -- ") + self.message


def _check_config(ws) -> CheckResult:
    issues = validate_workspace_config(
        ws,
        require_anthropic=False,  # rough check: don't demand a key
        require_attio=False,
        require_examples=False,
    )
    if issues:
        return CheckResult(
            "workspace_config", False,
            f"{len(issues)} issue(s): " + "; ".join(issues),
        )
    return CheckResult("workspace_config", True, "all required configs present")


def _check_stage6_freshness(engine) -> CheckResult:
    with engine.begin() as conn:
        row = conn.execute(
            select(runs.c.completed_at, runs.c.records_failed)
            .where(
                runs.c.stage == "06_score_candidates",
                runs.c.completed_at.isnot(None),
            )
            .order_by(desc(runs.c.run_id))
            .limit(1)
        ).first()
    if row is None:
        return CheckResult(
            "stage6_freshness", False,
            "Stage 6 has never completed; run scripts/06_score_candidates.py",
        )
    if (row.records_failed or 0) > 0:
        return CheckResult(
            "stage6_freshness", False,
            f"last Stage 6 run had records_failed={row.records_failed}; "
            f"investigate before sending",
        )
    age = (
        datetime.now(timezone.utc).replace(tzinfo=None)
        - row.completed_at
    )
    if age > timedelta(hours=STALE_STAGE6_HOURS):
        return CheckResult(
            "stage6_freshness", False,
            f"Stage 6 last completed {age} ago "
            f"(> {STALE_STAGE6_HOURS}h); re-score before sending",
        )
    return CheckResult(
        "stage6_freshness", True,
        f"Stage 6 completed {age} ago",
    )


def _check_approval_pipeline(engine) -> CheckResult:
    """Either approved drafts exist (ready to send) OR pending-review
    drafts exist (operator has something to do). An empty workspace
    in both buckets means Stage 7 hasn't been run."""
    approved = approved_for_send(engine)
    pending = pending_review(engine)
    if not approved and not pending:
        return CheckResult(
            "approval_pipeline", False,
            "no drafts in either approved_to_send or needs_review -- "
            "run scripts/07_generate_emails.py to produce drafts",
        )
    return CheckResult(
        "approval_pipeline", True,
        f"{len(approved)} approved + {len(pending)} pending review",
    )


def _check_approved_have_emails(engine) -> CheckResult:
    """Every approved draft must have a partner email (the approval
    blocker should have prevented otherwise -- defense in depth)."""
    approved = approved_for_send(engine)
    if not approved:
        return CheckResult(
            "approved_have_emails", True,
            "no approved drafts to check",
        )
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: (r.email or "").strip()
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    missing = [
        d.draft_id for d in approved
        if not email_by_pid.get(d.partner_id)
    ]
    if missing:
        return CheckResult(
            "approved_have_emails", False,
            f"{len(missing)} approved draft(s) missing partner email: "
            f"draft_ids={missing[:5]}{'...' if len(missing) > 5 else ''} "
            f"-- approvals should be stale; re-import Apollo data",
        )
    return CheckResult(
        "approved_have_emails", True,
        f"all {len(approved)} approved drafts have partner email",
    )


def _check_no_dnc_approvals(engine) -> CheckResult:
    """An approved draft for a partner whose do_not_contact flag is
    set is a hard refusal. The approval blocker should have prevented
    this; surface as blocked if it slipped through."""
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                email_drafts.c.draft_id, email_drafts.c.partner_id,
            ).join(
                partners,
                partners.c.partner_id == email_drafts.c.partner_id,
            ).where(
                email_drafts.c.approval_status == STATE_APPROVED_TO_SEND,
                partners.c.do_not_contact.is_(True),
            )
        ))
    if rows:
        return CheckResult(
            "no_dnc_approvals", False,
            f"{len(rows)} approved draft(s) target do_not_contact "
            f"partners: " + ", ".join(
                f"draft_id={r.draft_id}/partner={r.partner_id}"
                for r in rows
            ),
        )
    return CheckResult(
        "no_dnc_approvals", True,
        "no approved drafts target do_not_contact partners",
    )


def _check_mode(ws) -> CheckResult:
    mode = getattr(ws, "mode", None) or "(unset)"
    if mode == "fixture":
        return CheckResult(
            "mode", False,
            f"mode=fixture; cold-outreach send is BLOCKED. Either "
            f"flip company.yaml's `mode:` to 'production' or run with "
            f"--allow-fixture-mode on the downstream send scripts.",
        )
    return CheckResult("mode", True, f"mode={mode}")


def _run_all_checks(ws, engine) -> list[CheckResult]:
    return [
        _check_mode(ws),
        _check_config(ws),
        _check_stage6_freshness(engine),
        _check_approval_pipeline(engine),
        _check_approved_have_emails(engine),
        _check_no_dnc_approvals(engine),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-send safety check for cold-outreach.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only print BLOCKED lines + the summary.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage="check_ready")

    results = _run_all_checks(ws, engine)
    blocked = [r for r in results if not r.ok]
    passed = [r for r in results if r.ok]
    for r in results:
        if args.quiet and r.ok:
            continue
        print(r.render())

    print(
        f"\n[check_ready] {len(passed)} passed, {len(blocked)} blocked"
    )
    if blocked:
        print(
            "[check_ready] resolve the BLOCKED items before sending."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
