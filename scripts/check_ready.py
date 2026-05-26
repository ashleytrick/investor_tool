"""Pre-flight 'safe to ___?' check for the workspace.

Walks the operator-actionable preconditions for the chosen workflow
phase and prints OK / BLOCKED per check + a one-line summary at the
end. Designed for cron-friendly use: exits 0 when nothing is blocking,
non-zero when at least one check fails.

The phase is selected with `--for`:

  --for review   (default-friendly view: what's queued for me to do?)
                 OK with only pending-review drafts; no Gmail / Attio
                 reachability assumed.

  --for send     Pre-send gate -- requires at least ONE approved draft
                 AND every approved draft still passes the live gate.
                 Use this before Gmail/export/Attio.

  --for gmail    Pre-Gmail-push: send-mode checks + Gmail OAuth +
                 scheduling-link reachability.

  --for attio    Pre-Attio-sync: send-mode checks + Attio config /
                 credentials reachable.

Output format:
  [check_ready] {section}: OK / BLOCKED -- {reason}
  ...
  [check_ready] {N} checks passed, {M} blocked

Exit code 0 = safe to proceed; 1 = blocked; 2 = error running the
check itself.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, select

from core.approval.gate import can_approve_draft
from core.approval.persistence import approved_for_send, pending_review
from core.approval.state_machine import STATE_APPROVED_TO_SEND
from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    email_drafts, get_engine, partners, runs,
)
from core.deliverability import (
    configured_daily_cap, enforce_daily_approval_cap,
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
    """Review-mode pipeline check: either approved drafts exist OR
    pending-review drafts exist (operator has something to do). An
    empty workspace in both buckets means Stage 7 hasn't been run.
    Stricter check for send/gmail/attio modes lives in
    `_check_have_approved_drafts`.
    """
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


def _check_have_approved_drafts(engine) -> CheckResult:
    """Send/gmail/attio modes require at least one live approved draft.
    A workspace with 0 approved + 10 pending review looks ready under
    `_check_approval_pipeline` but has nothing for Gmail / export /
    Attio to ship -- this check refuses that state explicitly so
    `check_ready --for send` is a real green light."""
    approved = approved_for_send(engine)
    if not approved:
        return CheckResult(
            "have_approved_drafts", False,
            "0 drafts in approved_to_send -- approve at least one "
            "draft via scripts/approve_draft.py before sending",
        )
    return CheckResult(
        "have_approved_drafts", True,
        f"{len(approved)} approved draft(s) ready to ship",
    )


def _check_attio_config(ws) -> CheckResult:
    """Attio-mode reachability: workspace has an Attio config block and
    an ATTIO_API_KEY available. Doesn't make a network call -- the
    next Stage 8 run will. The point here is to refuse fast on
    `check_ready --for attio` when the workspace was never wired up.

    Both attio.yaml shapes (top-level `attio:` block OR a flat file)
    are accepted, matching core/attio_client.py:75. The API key is
    resolved through `ws.env()` so workspace .env files satisfy this
    check the same way the live Stage 8 will -- only checking
    os.environ would fire a false BLOCKED on workspaces that store
    the key in clients/<name>/.env.
    """
    attio = (ws.attio or {}) if hasattr(ws, "attio") else {}
    # Accept either {"attio": {...}} (nested) or a flat top-level
    # config; mirror core.attio_client.AttioClient.from_workspace.
    if isinstance(attio, dict):
        attio_cfg = attio.get("attio") if attio.get("attio") else attio
    else:
        attio_cfg = {}
    if not attio_cfg:
        return CheckResult(
            "attio_config", False,
            "config/attio.yaml is missing or empty -- Stage 8 has "
            "nothing to sync to",
        )
    api_key = (
        ws.env("ATTIO_API_KEY") if hasattr(ws, "env")
        else __import__("os").environ.get("ATTIO_API_KEY")
    )
    if not api_key:
        return CheckResult(
            "attio_config", False,
            "ATTIO_API_KEY env var is not set -- Stage 8 will refuse "
            "to authenticate",
        )
    return CheckResult(
        "attio_config", True,
        "attio.yaml present and ATTIO_API_KEY set",
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


def _check_approved_gate_clean(ws, engine, allow_example_domains: bool) -> CheckResult:
    """Re-run the canonical approval gate over every approved_to_send
    draft. This catches Slices 7-9 conditions that can regress AFTER
    the operator approved (DNC flipped on, verification went invalid,
    partner email cleared, qa_status revised by a Stage 7 re-run).

    The gate is the single source of truth -- using it here keeps
    check_ready in sync as the gate grows. respect_overrides=True so
    drafts the operator approved with --override-blockers don't get
    flagged again (the override is structurally persisted on the
    approval event).
    """
    approved = approved_for_send(engine)
    if not approved:
        return CheckResult(
            "approved_gate_clean", True, "no approved drafts to re-check",
        )
    stale: list[tuple[int, tuple[str, ...]]] = []
    overridden_count = 0
    for d in approved:
        gate = can_approve_draft(
            ws, engine, d.draft_id,
            allow_example_domains=allow_example_domains,
            respect_overrides=True,
        )
        if gate.overridden:
            overridden_count += 1
        if not gate.ok:
            stale.append((d.draft_id, gate.blockers))
    if not stale:
        msg = f"all {len(approved)} approved drafts still pass the gate"
        if overridden_count:
            msg += f" ({overridden_count} with operator override)"
        return CheckResult("approved_gate_clean", True, msg)
    sample = "; ".join(
        f"draft_id={did}: {', '.join(blockers[:2])}"
        + ("..." if len(blockers) > 2 else "")
        for did, blockers in stale[:3]
    )
    return CheckResult(
        "approved_gate_clean", False,
        f"{len(stale)} approved draft(s) now have live blockers; "
        f"re-approve or reject. Sample: {sample}",
    )


def _check_no_duplicate_recipients(engine) -> CheckResult:
    """Two approved drafts pointing at the same partner_email would
    produce two sends to the same person -- noisy + likely to bounce
    the second."""
    approved = approved_for_send(engine)
    if not approved:
        return CheckResult(
            "no_duplicate_recipients", True, "no approved drafts to check",
        )
    with engine.begin() as conn:
        email_by_pid = {
            r.partner_id: (r.email or "").strip().lower()
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.email),
            )
        }
    seen: dict[str, list[int]] = {}
    for d in approved:
        email = email_by_pid.get(d.partner_id) or ""
        if not email:
            continue  # covered by approved_have_emails
        seen.setdefault(email, []).append(d.draft_id)
    dupes = {e: ids for e, ids in seen.items() if len(ids) > 1}
    if not dupes:
        return CheckResult(
            "no_duplicate_recipients", True,
            f"{len(seen)} unique recipient(s) across approved drafts",
        )
    sample = ", ".join(
        f"{email!r} -> draft_ids={ids}"
        for email, ids in list(dupes.items())[:3]
    )
    return CheckResult(
        "no_duplicate_recipients", False,
        f"{len(dupes)} email(s) appear on >1 approved draft: {sample}",
    )


def _check_daily_cap_headroom(ws, engine) -> CheckResult:
    """Informational: how close are we to today's approval cap? Caller
    can refuse based on this OR proceed -- the cap exists to throttle
    cold sends, not to block them entirely.

    Finding 6: cap reads from company.yaml's
    `deliverability.daily_approval_cap` via configured_daily_cap()."""
    cap = configured_daily_cap(ws)
    blocked, count = enforce_daily_approval_cap(engine, cap=cap)
    if blocked:
        return CheckResult(
            "daily_cap_headroom", False,
            f"{count} approvals today / cap {cap} -- new approvals "
            f"refused until UTC rollover unless --override-cap. "
            f"Existing approved drafts can still send.",
        )
    return CheckResult(
        "daily_cap_headroom", True,
        f"{count} approvals today (cap {cap})",
    )


def _check_scheduling_link_reachable(ws) -> CheckResult:
    """Slice 15: HEAD-request the configured scheduling link with a
    short timeout. A 404 / DNS failure here = the link in every cold
    email is broken, which is the worst possible kind of silent
    failure (the operator only notices via reply absence).

    Soft check: when the link is missing OR uses an example/reserved
    TLD, we return OK ("nothing to check"). The production guard in
    `core/production_guards.py` already refuses to ship those.
    Reachability only matters once the link is a real URL.
    """
    co = (ws.company or {}).get("company") or {}
    link = (co.get("meeting_ask") or {}).get("preferred_scheduling_link") or ""
    link = link.strip()
    if not link:
        return CheckResult(
            "scheduling_link_reachable", True,
            "no scheduling link configured (production_guard catches this)",
        )
    if not link.startswith(("http://", "https://")):
        return CheckResult(
            "scheduling_link_reachable", False,
            f"scheduling link {link!r} is not HTTP(S); fix in company.yaml",
        )
    # Skip example / reserved TLDs -- the production guard refuses
    # those at send time, so they're not the operator's fault here.
    for suffix in (".example", ".test", ".invalid", ".localhost"):
        if suffix in link.lower():
            return CheckResult(
                "scheduling_link_reachable", True,
                f"skipping reachability for reserved-TLD link {link!r} "
                f"(production_guard refuses this at send time)",
            )
    # Some scheduling services (Calendly, SavvyCal) reject HEAD with
    # 403/405 even though GET works. Try HEAD first; on those two
    # status codes -- or any error -- fall back to a lightweight GET
    # before declaring the link broken.
    import urllib.request
    def _probe(method: str) -> tuple[int | None, str | None]:
        try:
            req = urllib.request.Request(link, method=method)
            # 5s is enough for any healthy scheduling service; we'd
            # rather report "slow / unreachable" than hang the operator.
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.getcode(), None
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except Exception as exc:  # noqa: BLE001 -- diverse URL/timeout errors
            return None, f"{type(exc).__name__}: {exc}"

    status, err = _probe("HEAD")
    if status in (403, 405) or status is None:
        # HEAD-hostile server or low-level error -- retry with GET.
        status, err = _probe("GET")
    if status is None:
        return CheckResult(
            "scheduling_link_reachable", False,
            f"GET {link} failed: {err}",
        )
    if status >= 400:
        return CheckResult(
            "scheduling_link_reachable", False,
            f"GET {link} -> {status}; recipients will hit a broken link",
        )
    return CheckResult(
        "scheduling_link_reachable", True,
        f"{link} -> {status}",
    )


def _check_gmail_oauth(ws) -> CheckResult:
    """Slice 15: confirm Gmail OAuth still works WITHOUT pushing a
    draft. Calls users.getProfile (the cheapest read in the
    gmail.compose scope). When credentials aren't set up, returns OK
    with a "not configured" message -- check_ready doesn't require
    Gmail; only `production` mode does via WorkspacePolicy.
    """
    try:
        from core.gmail_client import (
            GmailClient, GmailError, GmailNotConfigured,
        )
        client = GmailClient.from_workspace(ws)
    except GmailNotConfigured:
        return CheckResult(
            "gmail_oauth", True,
            "Gmail not linked (skipped); run connect_gmail.py if you "
            "intend to push drafts",
        )
    except ImportError as exc:
        return CheckResult(
            "gmail_oauth", False,
            f"google API libraries missing: {exc}",
        )
    try:
        profile = client.get_profile()
    except GmailError as exc:
        return CheckResult(
            "gmail_oauth", False,
            f"Gmail OAuth failed: {exc}. Re-run connect_gmail.py to "
            f"refresh the token.",
        )
    email = profile.get("emailAddress", "?")
    return CheckResult(
        "gmail_oauth", True,
        f"Gmail OAuth healthy (account={email})",
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


PHASES = ("review", "send", "gmail", "attio")


def _run_all_checks(
    ws, engine, *, phase: str, allow_example_domains: bool,
) -> list[CheckResult]:
    """Build the check list for the requested phase.

    review: minimal -- show what's queued, don't insist on approvals.
    send:   require at least one approved draft + every approved draft
            still passes the live gate.
    gmail:  send + Gmail OAuth + scheduling link.
    attio:  send + Attio config / credentials.
    """
    # Common baseline: every phase needs a valid workspace + fresh
    # Stage 6 data, and we always print the pipeline summary so the
    # operator knows what state they're in.
    checks: list[CheckResult] = [
        _check_mode(ws),
        _check_config(ws),
        _check_stage6_freshness(engine),
        _check_approval_pipeline(engine),
    ]
    if phase == "review":
        # The reviewer's view: that's it. Approval-side defense checks
        # only make sense when something is actually approved.
        return checks
    # send / gmail / attio share the "actually sendable" gate.
    checks.extend([
        _check_have_approved_drafts(engine),
        _check_approved_have_emails(engine),
        _check_no_dnc_approvals(engine),
        _check_approved_gate_clean(ws, engine, allow_example_domains),
        _check_no_duplicate_recipients(engine),
        _check_daily_cap_headroom(ws, engine),
    ])
    if phase == "gmail":
        checks.extend([
            _check_scheduling_link_reachable(ws),
            _check_gmail_oauth(ws),
        ])
    elif phase == "attio":
        checks.append(_check_attio_config(ws))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight safety check for cold-outreach.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--for", dest="phase", choices=PHASES, default="send",
        help="Which workflow phase to gate on. review = approval-queue "
             "snapshot (no approved drafts required); send = pre-send "
             "checks (requires approved drafts); gmail = send + Gmail "
             "OAuth + scheduling-link reachability; attio = send + "
             "Attio config. Default: send.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only print BLOCKED lines + the summary.",
    )
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Accept .example fixture data through the approval gate "
             "re-check. Useful for fixture smoke tests.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=f"check_ready --for {args.phase}")

    results = _run_all_checks(
        ws, engine,
        phase=args.phase,
        allow_example_domains=args.allow_example_domains,
    )
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
