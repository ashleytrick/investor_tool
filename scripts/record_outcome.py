"""Record an outcome for a partner without going through Attio.

Without Attio, the brief's "Outcome tracking via Attio (or manual CSV import)"
path was missing the CSV-import side. This script is it. Outcomes appended
here feed the same monthly_learning_report aggregations as Attio-sourced
outcomes.

Examples:
  # One-off, after a reply.
  uv run scripts/record_outcome.py --partner-id NAME \\
      --status replied --reply-type asked_for_deck

  # Booked a meeting.
  uv run scripts/record_outcome.py --partner-id NAME \\
      --status meeting_booked --reply-type booked \\
      --meeting-booked --meeting-date 2026-06-12 --meeting-outcome pending

  # Batch from a CSV (cols: partner_id, status, reply_type,
  # meeting_booked, meeting_date, meeting_outcome).
  uv run scripts/record_outcome.py --from-csv outcomes.csv
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, outcomes, partners
from core.runs import RunLogger

STAGE = "record_outcome"

STATUS_VALUES = {
    "draft", "ready_to_send", "sent", "replied",
    "meeting_booked", "dead", "warm_path_needed",
}
REPLY_TYPE_VALUES = {
    "no_response", "booked", "asked_for_deck", "passed_too_early",
    "passed_category", "wrong_stage", "asked_for_more_info",
    "referred_to_colleague", "warm_intro_requested",
}
MEETING_OUTCOME_VALUES = {"pitched", "no_show", "advanced", "killed", "pending"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"--meeting-date must be YYYY-MM-DD; got {s!r}")


def _validate_choices(*, status, reply_type, meeting_outcome):
    if status and status not in STATUS_VALUES:
        raise SystemExit(
            f"--status {status!r} not in allowed set: {sorted(STATUS_VALUES)}"
        )
    if reply_type and reply_type not in REPLY_TYPE_VALUES:
        raise SystemExit(
            f"--reply-type {reply_type!r} not in allowed set: "
            f"{sorted(REPLY_TYPE_VALUES)}"
        )
    if meeting_outcome and meeting_outcome not in MEETING_OUTCOME_VALUES:
        raise SystemExit(
            f"--meeting-outcome {meeting_outcome!r} not in allowed set: "
            f"{sorted(MEETING_OUTCOME_VALUES)}"
        )


def _insert(conn, *, partner_id, status, reply_type, meeting_booked,
            meeting_date, meeting_outcome):
    conn.execute(outcomes.insert().values(
        partner_id=partner_id,
        outreach_status=status,
        reply_type=reply_type,
        meeting_booked=bool(meeting_booked),
        meeting_date=meeting_date,
        meeting_outcome=meeting_outcome,
        synced_from_attio_at=_now(),
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a partner outcome.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", default=None)
    parser.add_argument("--status", default=None, choices=sorted(STATUS_VALUES))
    parser.add_argument("--reply-type", default=None,
                        choices=sorted(REPLY_TYPE_VALUES))
    parser.add_argument("--meeting-booked", action="store_true")
    parser.add_argument("--meeting-date", default=None,
                        help="YYYY-MM-DD; only with --meeting-booked.")
    parser.add_argument("--meeting-outcome", default=None,
                        choices=sorted(MEETING_OUTCOME_VALUES))
    parser.add_argument("--from-csv", default=None,
                        help="Batch import; CSV with columns "
                             "partner_id, status, reply_type, meeting_booked, "
                             "meeting_date, meeting_outcome.")
    args = parser.parse_args()

    if not args.from_csv and not args.partner_id:
        parser.error("--partner-id is required unless --from-csv is used")

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    # Build lookup of known partner_ids to validate against.
    with engine.begin() as conn:
        known = {
            r.partner_id
            for r in conn.execute(select(partners.c.partner_id))
        }

    with RunLogger(engine, ws.name, STAGE) as run:
        if args.from_csv:
            path = pathlib.Path(args.from_csv)
            if not path.exists():
                print(f"[record_outcome] file not found: {path}")
                run.failed = 1
                return 2
            with path.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                with engine.begin() as conn:
                    for row in reader:
                        run.processed += 1
                        pid = (row.get("partner_id") or "").strip()
                        if not pid:
                            run.skipped += 1
                            continue
                        if pid not in known:
                            run.failed += 1
                            run.log_error(
                                pid, "unknown_partner",
                                "partner_id not in partners table",
                            )
                            continue
                        try:
                            _validate_choices(
                                status=(row.get("status") or "").strip() or None,
                                reply_type=(row.get("reply_type") or "").strip() or None,
                                meeting_outcome=(row.get("meeting_outcome") or "").strip() or None,
                            )
                            _insert(
                                conn,
                                partner_id=pid,
                                status=(row.get("status") or "").strip() or None,
                                reply_type=(row.get("reply_type") or "").strip() or None,
                                meeting_booked=(row.get("meeting_booked") or "").strip().lower() in ("true", "1", "yes"),
                                meeting_date=_parse_date(
                                    (row.get("meeting_date") or "").strip() or None
                                ),
                                meeting_outcome=(row.get("meeting_outcome") or "").strip() or None,
                            )
                            run.succeeded += 1
                        except SystemExit as exc:
                            run.failed += 1
                            run.log_error(pid, "validation", str(exc))
            print(
                f"[record_outcome] from-csv: processed={run.processed} "
                f"ok={run.succeeded} failed={run.failed} skipped={run.skipped}"
            )
            return 0

        # Single-record path.
        if args.partner_id not in known:
            print(f"[record_outcome] unknown partner_id: {args.partner_id!r}")
            run.failed = 1
            run.log_error(args.partner_id, "unknown_partner", "not in partners table")
            return 2
        _validate_choices(
            status=args.status,
            reply_type=args.reply_type,
            meeting_outcome=args.meeting_outcome,
        )
        with engine.begin() as conn:
            _insert(
                conn,
                partner_id=args.partner_id,
                status=args.status,
                reply_type=args.reply_type,
                meeting_booked=args.meeting_booked,
                meeting_date=_parse_date(args.meeting_date),
                meeting_outcome=args.meeting_outcome,
            )
        run.processed = 1
        run.succeeded = 1
        print(
            f"[record_outcome] {args.partner_id}: "
            f"status={args.status} reply_type={args.reply_type} "
            f"meeting_booked={args.meeting_booked}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
