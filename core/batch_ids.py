"""Pipeline-spanning batch lineage (Issue #19 / REFACTOR_PLAN item 18 cont).

Operators that want "every row this Tuesday's pipeline pass touched"
mint a batch via this module then pass the resulting id to each
subsequent stage as --pipeline-batch.

Today the helper covers the registry and the link onto the `runs`
table. Future slices extend the link to source_snapshots, signals,
deal_attributions, partner_score_summaries, attio_sync_log, outcomes
so a single JOIN walks the lineage end-to-end.

Usage:

    # operator opens a batch (or scripts/new_pipeline_batch.py CLI):
    from core.batch_ids import create_pipeline_batch
    with engine.begin() as conn:
        batch_id = create_pipeline_batch(
            conn, workspace="oko_seed", operator="ashley",
            notes="weekly Tuesday pass",
        )
    # pass batch_id to each stage as --pipeline-batch.
    # stages pick up the flag via stage_run() and stamp it on the runs
    # row automatically.

The batch id is a 16-byte hex string (`secrets.token_hex(16)`) -- 128
bits is more than enough collision-resistance for the volume we
expect (one per operator per pipeline pass).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from core.db import pipeline_batches


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mint_batch_id() -> str:
    """Generate a fresh 128-bit hex batch id. Distinct from Stage 7's
    `batch_id` (which is local to Stage 7 + uses a different namespace)
    by virtue of the `pipeline_` semantic context -- operators query
    via the pipeline_batches table, not the per-stage batch_id."""
    return secrets.token_hex(16)


def create_pipeline_batch(
    conn: Any,
    *,
    workspace: str,
    operator: str | None = None,
    notes: str | None = None,
) -> str:
    """Open a pipeline batch row + return the new batch_id. Caller
    threads the id through every subsequent stage's --pipeline-batch
    flag. Returns the new batch_id."""
    batch_id = mint_batch_id()
    conn.execute(pipeline_batches.insert().values(
        batch_id=batch_id,
        workspace=workspace,
        started_at=_now(),
        operator=operator,
        notes=notes,
    ))
    return batch_id


def finalize_pipeline_batch(conn: Any, *, batch_id: str) -> None:
    """Stamp completed_at on the batch. Stage 8 (or the operator's
    cron wrapper) calls this once the last stage finishes so a batch's
    timeline is bounded. Idempotent -- a second call overwrites
    completed_at, which is fine; the most recent timestamp wins."""
    conn.execute(
        update(pipeline_batches)
        .where(pipeline_batches.c.batch_id == batch_id)
        .values(completed_at=_now())
    )


def batch_exists(conn: Any, batch_id: str) -> bool:
    """True iff `batch_id` is registered in pipeline_batches. Used by
    stage_run to refuse a --pipeline-batch value that doesn't exist
    (catches typos before they produce orphan runs with bogus
    pipeline_batch_id)."""
    return conn.execute(
        select(pipeline_batches.c.batch_id).where(
            pipeline_batches.c.batch_id == batch_id
        )
    ).first() is not None
