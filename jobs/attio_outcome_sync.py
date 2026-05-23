"""Pull recent partner-record modifications from Attio into the local
`outcomes` table.

Queries Attio's /v2/objects/people/records/query with a last_modified filter,
maps each returned record_id back to the local partner_id via
partners.attio_record_id, and appends a row to `outcomes` with the
outreach_status / reply_type / meeting_* fields. Append-only: each sync
produces a snapshot; the monthly learning report consumes the most recent
row per partner.

If the workspace has no attio.yaml or no ATTIO_API_KEY, exits 0 with a clear
skip message. Re-runs are safe; the brief schema has no unique constraint on
outcomes, so multiple syncs build a history of state changes.

Run: uv run python jobs/attio_outcome_sync.py --workspace clients/{name}
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.attio_client import AttioClient, AttioError, AttioNotConfigured
from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import get_engine, outcomes, partners
from core.runs import RunLogger

STAGE = "attio_outcome_sync"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scalar(values: dict, slug: str):
    v = values.get(slug)
    if not v:
        return None
    if isinstance(v, list):
        return v[0].get("value") if v else None
    return v


def _option_title(values: dict, slug: str) -> str | None:
    v = values.get(slug)
    if not v:
        return None
    try:
        return v[0]["option"]["title"]
    except (KeyError, IndexError, TypeError):
        return None


def _date(values: dict, slug: str) -> date | None:
    raw = _scalar(values, slug)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull outcomes from Attio.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--lookback-days", type=int, default=7,
        help="Pull records modified in the last N days (default 7).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    cfg = ws.attio or {}
    attio_cfg = cfg.get("attio") or cfg

    with RunLogger(engine, ws.name, STAGE) as run:
        if not attio_cfg:
            print(f"[outcome_sync] no attio.yaml in workspace {ws.name!r}; skipping")
            run.skipped = 1
            return 0
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            print(f"[outcome_sync] {exc}; skipping")
            run.skipped = 1
            return 0

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
            return 0

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
            return 2
        print(f"[outcome_sync] pulled {len(records)} modified record(s) from Attio")

        for rec in records:
            run.processed += 1
            rec_id = (rec.get("id") or {}).get("record_id")
            try:
                pid = attio_to_partner.get(rec_id)
                if not pid:
                    run.skipped += 1
                    continue
                values = rec.get("values", {})
                row = {
                    "partner_id": pid,
                    "outreach_status": _option_title(values, "outreach_status"),
                    "reply_type": _option_title(values, "reply_type"),
                    "meeting_booked": bool(_scalar(values, "meeting_booked")),
                    "meeting_date": _date(values, "meeting_date"),
                    "meeting_outcome": _option_title(values, "meeting_outcome"),
                    "synced_from_attio_at": _now(),
                }
                with engine.begin() as conn:
                    conn.execute(outcomes.insert().values(**row))
                run.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - logged, continue
                run.failed += 1
                run.log_error(rec_id or "?", type(exc).__name__, str(exc))

        client.close()
        print(
            f"[outcome_sync] synced {run.succeeded} outcome row(s); "
            f"skipped={run.skipped} failed={run.failed}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
