"""Populate partners.email so create_gmail_drafts.py knows where to send.

Examples:
  uv run scripts/set_partner_email.py --partner-id NAME --email j@fund.com
  uv run scripts/set_partner_email.py --from-csv emails.csv
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, partners
from core.runs import RunLogger
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

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    print_banner(ws, stage=STAGE)

    with engine.begin() as conn:
        known = {r.partner_id for r in conn.execute(select(partners.c.partner_id))}

    with RunLogger(engine, ws.name, STAGE) as run:
        rows: list[tuple[str, str]] = []
        if args.from_csv:
            path = pathlib.Path(args.from_csv)
            if not path.exists():
                print(f"[set_partner_email] file not found: {path}")
                run.failed = 1
                return 2
            with path.open(encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    pid = (r.get("partner_id") or "").strip()
                    email = (r.get("email") or "").strip()
                    if pid and email:
                        rows.append((pid, email))
        else:
            rows = [(args.partner_id, args.email)]

        with engine.begin() as conn:
            for pid, email in rows:
                run.processed += 1
                if pid not in known:
                    run.failed += 1
                    run.log_error(pid, "unknown_partner", "not in partners table")
                    continue
                # Batch 35: validate email shape before writing. A typo
                # ("priya at northbeam") used to write garbage that only
                # failed at Gmail draft time with a cryptic API error.
                if not _looks_like_email(email):
                    run.failed += 1
                    run.log_error(
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
                run.succeeded += 1
                print(f"[set_partner_email] {pid} -> {email}")

        print(
            f"[set_partner_email] processed={run.processed} "
            f"ok={run.succeeded} failed={run.failed}"
        )
        any_failed = run.failed > 0

    # Batch 35: non-zero exit when any row failed (unknown partner, bad
    # email shape) so cron / wrapping scripts notice.
    return 2 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
