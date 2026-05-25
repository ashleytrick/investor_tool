"""Pull recent partner-record modifications from Attio into the local
`outcomes` table.

Queries Attio's /v2/objects/people/records/query with a last_modified
filter and hands every returned record to the Attio outcome adapter
(core/outcomes/attio_adapter.py). Each `OutcomeEvent` then flows
through the source-neutral persistence layer
(core/outcomes/persistence.py), which dedups via external_event_id
+ skips state-unchanged events before insert.

If the workspace has no attio.yaml or no ATTIO_API_KEY, exits 0 with
a clear skip message. Re-runs are safe; the external_event_id unique
index guarantees that retries / cron overlaps don't double-insert.

Run: uv run python jobs/attio_outcome_sync.py --workspace clients/{name}
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.attio_client import AttioClient, AttioError, AttioNotConfigured
from core.config_loader import add_workspace_arg, load_workspace
from core.db import partners
from core.outcomes.attio_adapter import attio_record_to_event
from core.outcomes.persistence import persist_outcome_event
from core.stage_runner import stage_run

STAGE = "attio_outcome_sync"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull outcomes from Attio.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Pull records modified in the last N days (default 7).",
    )
    args = parser.parse_args()

    # Refactor sweep: stage_run() boilerplate collapse. require_attio is
    # dynamic (only enforce when workspace declares attio.yaml).
    _require_attio = bool(load_workspace(args.workspace).attio)
    with stage_run(args, stage=STAGE, require_attio=_require_attio,
                   require_llm=False) as ctx:
        ws, engine, run = ctx.ws, ctx.engine, ctx.run
        cfg = ws.attio or {}
        attio_cfg = cfg.get("attio") or cfg
        if not attio_cfg:
            print(f"[outcome_sync] no attio.yaml in workspace {ws.name!r}; skipping")
            run.skipped = 1
            return ctx.exit_code
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            print(f"[outcome_sync] {exc}; skipping")
            run.skipped = 1
            return ctx.exit_code

        with engine.begin() as conn:
            attio_to_partner: dict[str, str] = {
                r.attio_record_id: r.partner_id
                for r in conn.execute(
                    select(partners.c.partner_id, partners.c.attio_record_id)
                    .where(partners.c.attio_record_id.isnot(None))
                )
            }
        if not attio_to_partner:
            print("[outcome_sync] no partners with attio_record_id; "
                  "run Stage 8 sync first")
            run.skipped = 1
            client.close()
            return ctx.exit_code

        person_object = (attio_cfg.get("objects") or {}).get("partners", "people")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=args.lookback_days)
        ).isoformat()

        try:
            # Paginated pull -- the previous single-page limit=100 was a
            # silent ceiling: any workspace with >100 modified people in
            # the lookback dropped outcomes and poisoned the learning loop.
            records = client.query_records_all(
                person_object,
                {"last_modified_at": {"$gte": cutoff}},
                page_size=100,
            )
        except AttioError as exc:
            print(f"[outcome_sync] Attio query failed: {exc}")
            run.failed = 1
            run.log_error("__query__", "AttioError", str(exc))
            client.close()
            return ctx.exit_code
        print(f"[outcome_sync] pulled {len(records)} modified record(s) from Attio")

        observed_at = _now()
        for rec in records:
            with run.attempt():
                rec_id = (rec.get("id") or {}).get("record_id")
                try:
                    event = attio_record_to_event(
                        rec, attio_to_partner, observed_at,
                    )
                    if event is None:
                        # Record doesn't map to a known local partner
                        # (the workspace hasn't synced this partner via
                        # Stage 8 yet, or shape drift on Attio's side).
                        run.skip()
                        continue
                    outcome_id = persist_outcome_event(engine, event)
                    if outcome_id is None:
                        # Dedup hit -- either external_event_id already
                        # exists OR the state matches the latest row.
                        run.skip()
                        continue
                except Exception as exc:  # noqa: BLE001 - logged, continue
                    run.fail(rec_id or "?", type(exc).__name__, str(exc))

        client.close()
        print(
            f"[outcome_sync] synced {run.succeeded} outcome row(s); "
            f"skipped={run.skipped} failed={run.failed}"
        )
        # Batch 35: non-zero exit when any per-record sync failed so
        # cron / wrapping scripts notice partial sync failures.
        # ctx.exit_code surfaces run.failed > 0 as exit 2.

    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
