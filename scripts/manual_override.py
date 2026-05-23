"""Set or clear the manual override flags on a partner without touching SQL.

The CSV review queue is read-only from the operator's side (it gets overwritten
by each Stage 7 run). To express judgment ("don't re-score this partner",
"force-promote this one to ready_to_send", "warm path exists, don't cold this
person") the operator needs to flip flags in partner_score_summaries /
partners. This script is the supported interface; routine Stage 6 runs respect
the flags it sets and Stage 7 honors warm_path_available.

Examples:
  # Pin scores on a partner you hand-tuned.
  uv run scripts/manual_override.py --partner-id NAME --score \\
      --reason "hand-curated after meeting"

  # Force-promote (or force-demote) recommended_to_send.
  uv run scripts/manual_override.py --partner-id NAME --recommended \\
      --reason "champion at fund; bypass criterion 4"

  # Mark warm path; Stage 6 will set outreach_status=warm_path_needed instead.
  uv run scripts/manual_override.py --partner-id NAME --warm-path \\
      --warm-path-contact "ashley@... knows them"

  # Clear all overrides on a partner.
  uv run scripts/manual_override.py --partner-id NAME --clear

  # Inspect what's overridden across the workspace.
  uv run scripts/manual_override.py --list
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, partner_score_summaries, partners
from core.runs import RunLogger

STAGE = "manual_override"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set/clear manual overrides.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="List all partners with any override flag set.")
    g.add_argument("--clear", action="store_true",
                   help="Clear all overrides on --partner-id.")
    g.add_argument("--score", action="store_true",
                   help="Set manual_score_override=TRUE on --partner-id.")
    g.add_argument("--recommended", action="store_true",
                   help="Set manual_recommended_override=TRUE on --partner-id.")
    g.add_argument("--warm-path", action="store_true",
                   help="Set partners.warm_path_available=TRUE on --partner-id.")

    parser.add_argument("--partner-id", default=None,
                        help="Target partner_id (required for non-list ops).")
    parser.add_argument("--reason", default=None,
                        help="Required for --score / --recommended / --warm-path.")
    parser.add_argument("--warm-path-contact", default=None,
                        help="Optional note: who has the warm intro.")
    args = parser.parse_args()

    if not args.list and not args.partner_id:
        parser.error("--partner-id is required unless --list")
    requires_reason = args.score or args.recommended or args.warm_path
    if requires_reason and not args.reason:
        parser.error("--reason is required when setting an override")

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with RunLogger(engine, ws.name, STAGE) as run:
        if args.list:
            with engine.begin() as conn:
                summary_rows = list(conn.execute(
                    select(
                        partner_score_summaries.c.partner_id,
                        partner_score_summaries.c.manual_score_override,
                        partner_score_summaries.c.manual_recommended_override,
                        partner_score_summaries.c.manual_override_reason,
                    ).where(
                        (partner_score_summaries.c.manual_score_override.is_(True))
                        | (partner_score_summaries.c.manual_recommended_override.is_(True))
                    )
                ))
                warm_rows = list(conn.execute(
                    select(
                        partners.c.partner_id, partners.c.name,
                        partners.c.warm_path_contact,
                    ).where(partners.c.warm_path_available.is_(True))
                ))
            if not summary_rows and not warm_rows:
                print("[overrides] none set in this workspace")
            for r in summary_rows:
                flags = []
                if r.manual_score_override:
                    flags.append("score")
                if r.manual_recommended_override:
                    flags.append("recommended")
                print(
                    f"[overrides] {r.partner_id}: {'+'.join(flags)} | "
                    f"reason={r.manual_override_reason!r}"
                )
            for r in warm_rows:
                print(
                    f"[overrides] {r.partner_id} ({r.name}): warm_path | "
                    f"contact={r.warm_path_contact!r}"
                )
            run.processed = len(summary_rows) + len(warm_rows)
            run.succeeded = run.processed
            return 0

        pid = args.partner_id
        run.processed = 1

        if args.warm_path:
            with engine.begin() as conn:
                existing = conn.execute(
                    select(partners.c.partner_id).where(partners.c.partner_id == pid)
                ).first()
                if not existing:
                    print(f"[overrides] partner {pid!r} not found in partners table")
                    run.failed = 1
                    run.log_error(pid, "not_found", "no such partner")
                    return 2
                conn.execute(
                    partners.update().where(partners.c.partner_id == pid).values(
                        warm_path_available=True,
                        warm_path_contact=args.warm_path_contact,
                        last_updated=_now(),
                    )
                )
            print(f"[overrides] {pid}: warm_path=TRUE; reason logged: {args.reason!r}")
            run.note(f"warm_path set on {pid}: {args.reason!r}")
            run.succeeded = 1
            return 0

        if args.clear:
            with engine.begin() as conn:
                conn.execute(
                    partner_score_summaries.update()
                    .where(partner_score_summaries.c.partner_id == pid)
                    .values(
                        manual_score_override=False,
                        manual_recommended_override=False,
                        manual_override_reason=None,
                    )
                )
                conn.execute(
                    partners.update().where(partners.c.partner_id == pid).values(
                        warm_path_available=None,
                        warm_path_contact=None,
                    )
                )
            print(f"[overrides] {pid}: all overrides cleared")
            run.note(f"cleared overrides on {pid}")
            run.succeeded = 1
            return 0

        # --score or --recommended
        update = {"manual_override_reason": args.reason}
        if args.score:
            update["manual_score_override"] = True
            label = "manual_score_override=TRUE"
        else:
            update["manual_recommended_override"] = True
            label = "manual_recommended_override=TRUE"
        with engine.begin() as conn:
            existing = conn.execute(
                select(partner_score_summaries.c.partner_id).where(
                    partner_score_summaries.c.partner_id == pid
                )
            ).first()
            if not existing:
                print(
                    f"[overrides] partner_score_summaries row not found for "
                    f"{pid!r}. Run Stage 6 first."
                )
                run.failed = 1
                run.log_error(pid, "not_found", "no summary row")
                return 2
            conn.execute(
                partner_score_summaries.update()
                .where(partner_score_summaries.c.partner_id == pid)
                .values(**update)
            )
        print(f"[overrides] {pid}: {label}; reason={args.reason!r}")
        run.note(f"{label} on {pid}: {args.reason!r}")
        run.succeeded = 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
