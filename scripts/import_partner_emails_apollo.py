"""Import partner emails from a CSV (Apollo or any enrichment tool).

CSV must have at minimum two columns:
  partner_id    -- must match an existing partners row
  email         -- the enriched email address

Optional columns the importer reads (everything else is ignored):
  current_email -- if present, used as a sanity check (skip rows
                   where current_email matches what's already in
                   the DB so re-imports are no-ops)

Conflict semantics:
  - Empty imported email   -> skip row silently (no-op)
  - Identical existing     -> skip row silently (no-op)
  - Existing email empty   -> WRITE the new email
  - Existing email differs -> CONFLICT
      - default: refuse with a conflict row written to run_errors
      - --overwrite: replace the existing email AND flip any
        approved_to_send drafts for that partner to
        stale_after_approval (the Slice 1 invalidation rule:
        partner email changed after approval -> stale)

Validation (via core.csv_ingest):
  - Missing 'partner_id' or 'email' column -> hard fail
  - partner_id not in partners table       -> row_error (skip)
  - email doesn't look like an email       -> row_error (skip)
  - duplicate partner_id in the CSV        -> row_error on the 2nd+

Usage:
  uv run scripts/import_partner_emails_apollo.py \\
      --workspace clients/{name} --from-csv apollo_enriched.csv
  uv run scripts/import_partner_emails_apollo.py \\
      --workspace clients/{name} --from-csv apollo_enriched.csv \\
      --overwrite   # required when imported email differs from existing
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.approval.persistence import mark_stale
from core.approval.state_machine import (
    STATE_APPROVED_TO_SEND, TRIGGER_EMAIL_CHANGED,
)
from core.config_loader import add_workspace_arg
from core.csv_ingest import (
    CsvIngestSchema, ingest_csv, in_set, looks_like_email, require_field,
)
from core.db import email_drafts, partners
from core.operator_command import operator_command_run

STAGE = "import_partner_emails_apollo"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import partner emails from an Apollo CSV.",
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--from-csv", required=True,
        help="Path to the Apollo CSV (partner_id, email).",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Allow overwriting an existing partner email when the "
             "imported value differs. Without this flag, conflicts "
             "are reported and the row is skipped.",
    )
    args = parser.parse_args()

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run

        with engine.begin() as conn:
            known_pids = {
                r.partner_id for r in conn.execute(
                    select(partners.c.partner_id),
                )
            }
            existing_email_by_pid = {
                r.partner_id: (r.email or "").strip().lower()
                for r in conn.execute(
                    select(partners.c.partner_id, partners.c.email),
                )
            }

        schema = CsvIngestSchema(
            required_headers={"partner_id", "email"},
            row_validators=(
                require_field("partner_id"),
                require_field("email"),
                in_set("partner_id", known_pids, error_type="unknown_partner"),
                looks_like_email("email"),
            ),
        )
        path = pathlib.Path(args.from_csv)
        result = ingest_csv(path, schema)

        if not path.exists() or result.missing_headers:
            msg = (
                f"file not found: {path}" if not path.exists()
                else f"CSV missing required column(s): "
                     f"{result.missing_headers}"
            )
            print(f"[apollo_import] REFUSED: {msg}")
            ctx.usage_error(msg)
            return ctx.exit_code

        for err in result.row_errors:
            run.log_error(err.record_id, err.error_type,
                          f"row {err.row_num}: {err.message}")
            run.failed += 1
            print(
                f"[apollo_import] row {err.row_num}: "
                f"{err.error_type}: {err.message}"
            )

        # Process well-formed rows.
        written = 0
        conflicts = 0
        no_op = 0
        for row in result.rows:
            with run.attempt():
                pid = row["partner_id"]
                new_email = row["email"].strip()
                if not new_email:
                    run.skip()
                    continue
                existing = existing_email_by_pid.get(pid, "")
                if existing and existing == new_email.lower():
                    # Exact match -- no-op.
                    no_op += 1
                    run.skip()
                    continue
                if existing and existing != new_email.lower():
                    if not args.overwrite:
                        conflicts += 1
                        msg = (
                            f"existing email {existing!r} differs from "
                            f"imported {new_email!r}; pass --overwrite "
                            f"to replace"
                        )
                        run.log_error(pid, "email_conflict", msg)
                        run.fail(pid, "email_conflict", msg)
                        print(f"[apollo_import] CONFLICT {pid}: {msg}")
                        continue
                    # --overwrite: write the new email AND flip
                    # approved drafts to stale_after_approval (Slice 1
                    # invalidation rule: partner email changed).
                    _overwrite_email_and_stale_approvals(
                        engine, pid=pid, new_email=new_email,
                    )
                    written += 1
                    print(
                        f"[apollo_import] OVERWROTE {pid}: "
                        f"{existing!r} -> {new_email!r} "
                        f"(approved drafts marked stale)"
                    )
                    continue
                # No existing email: write fresh.
                with engine.begin() as conn:
                    conn.execute(
                        partners.update()
                        .where(partners.c.partner_id == pid)
                        .values(email=new_email, last_updated=_now())
                    )
                written += 1
                print(f"[apollo_import] {pid} -> {new_email}")

        print(
            f"[apollo_import] wrote={written} conflicts={conflicts} "
            f"no_op={no_op} row_errors={len(result.row_errors)}"
        )
        # Non-zero exit when conflicts or row errors landed so cron /
        # wrappers notice; pure no-op runs (every row already matched)
        # exit clean. ctx.exit_code maps run.failed (>0) -> 2.
        return ctx.exit_code


def _overwrite_email_and_stale_approvals(
    engine, *, pid: str, new_email: str,
) -> None:
    """Write the new email + invalidate any approved drafts for this
    partner. The state-machine rule: when a partner email changes
    after approval, the recipient is no longer who was approved, so
    the approval is stale."""
    with engine.begin() as conn:
        conn.execute(
            partners.update()
            .where(partners.c.partner_id == pid)
            .values(email=new_email, last_updated=_now())
        )
        approved_drafts = list(conn.execute(
            select(email_drafts.c.draft_id).where(
                email_drafts.c.partner_id == pid,
                email_drafts.c.approval_status == STATE_APPROVED_TO_SEND,
            )
        ))
    for d in approved_drafts:
        mark_stale(
            engine,
            draft_id=int(d.draft_id),
            partner_id=pid,
            trigger=TRIGGER_EMAIL_CHANGED,
            notes=f"new email: {new_email}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
