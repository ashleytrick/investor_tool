"""Populate partners.email so create_gmail_drafts.py knows where to send.

Examples:
  uv run scripts/set_partner_email.py --partner-id NAME --email j@fund.com
  uv run scripts/set_partner_email.py --from-csv emails.csv
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.config_loader import add_workspace_arg
from core.csv_ingest import (
    CsvIngestSchema, ingest_csv, in_set, looks_like_email, require_field,
)
from core.db import partners
from core.operator_command import operator_command_run
from core.validate_config import _looks_like_email

STAGE = "set_partner_email"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set partner email address.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", default=None)
    parser.add_argument("--email", default=None)
    parser.add_argument("--from-csv", default=None,
                        help="CSV with columns: partner_id, email")
    args = parser.parse_args()

    if not args.from_csv and not (args.partner_id and args.email):
        parser.error("--partner-id AND --email required, unless --from-csv used")

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run

        with engine.begin() as conn:
            known = {r.partner_id for r in conn.execute(select(partners.c.partner_id))}

        rows: list[tuple[str, str]] = []
        if args.from_csv:
            path = pathlib.Path(args.from_csv)
            # core/csv_ingest (Refactor item 4) handles header validation,
            # row-level partner_id + email shape checks, and produces a
            # RowError per bad row. Failures land in run_errors and bump
            # run.failed via the loop below.
            schema = CsvIngestSchema(
                required_headers={"partner_id", "email"},
                row_validators=(
                    require_field("partner_id"),
                    require_field("email"),
                    in_set("partner_id", known,
                           error_type="unknown_partner"),
                    looks_like_email("email"),
                ),
            )
            result = ingest_csv(path, schema)
            if not path.exists() or result.missing_headers:
                msg = (
                    f"file not found: {path}" if not path.exists()
                    else f"CSV missing required column(s): "
                         f"{result.missing_headers}"
                )
                print(f"[set_partner_email] {msg}")
                ctx.usage_error(msg)
                return ctx.exit_code
            for err in result.row_errors:
                run.log_error(err.record_id, err.error_type, err.message)
                run.failed += 1
                print(
                    f"[set_partner_email] row {err.row_num}: "
                    f"{err.error_type}: {err.message}"
                )
            rows = [(r["partner_id"], r["email"]) for r in result.rows]
        else:
            rows = [(args.partner_id, args.email)]

        with engine.begin() as conn:
            for pid, email in rows:
                with run.attempt():
                    # Single-record path still needs the same checks; CSV
                    # path has already validated its rows via ingest_csv.
                    if pid not in known:
                        run.fail(pid, "unknown_partner", "not in partners table")
                        continue
                    if not _looks_like_email(email):
                        run.fail(
                            pid, "invalid_email",
                            f"{email!r} does not look like an email address",
                        )
                        print(
                            f"[set_partner_email] {pid}: REFUSED -- "
                            f"{email!r} is not a valid email shape"
                        )
                        continue
                    conn.execute(
                        partners.update().where(partners.c.partner_id == pid).values(
                            email=email, last_updated=_now(),
                        )
                    )
                    print(f"[set_partner_email] {pid} -> {email}")

        print(
            f"[set_partner_email] processed={run.processed} "
            f"ok={run.succeeded} failed={run.failed}"
        )

    # Non-zero exit when any row failed (unknown partner, bad email
    # shape) so cron / wrapping scripts notice. ctx.exit_code maps
    # run.failed > 0 -> OPERATIONAL_FAILURE = 2.
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
