"""Stage 6 persistence: write partner_score_summaries + per-axis scores
+ force_refresh_log entries (Refactor item 7 / 13).

Splits the DB-write half of Stage 6 out of the script body. The
script collects inputs and computes derived values; this module owns
the actual SQL.

Two top-level operations:

  - persist_partner_score(): upsert partner_score_summaries, replace
    the per-axis `scores` rows for the partner. Idempotent: a re-run
    on identical inputs produces no row deltas (the upsert key is
    partner_id; the per-axis delete+insert is sequenced inside the
    same transaction so partial failure rolls back).
  - log_force_refresh_diff(): when --force-rescore overrides a row
    that has a manual override flag set, write one
    force_refresh_log row per CHANGED field so the audit trail
    captures who-overwrote-what-why. Skipped when no override was
    active or nothing actually changed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete

from core.db import force_refresh_log, partner_score_summaries, scores, upsert


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_force_refresh_diff(
    engine: Any,
    *,
    partner_id: str,
    existing: Any,
    new_values: dict,
    reason: str,
    skip_fields: tuple[str, ...] = ("scored_at",),
) -> int:
    """Write one force_refresh_log row per field where the existing
    row's value differs from the new value. `existing` is the row
    object loaded from partner_score_summaries; `new_values` is the
    dict that will be upserted. Returns the count of rows written.
    """
    written = 0
    with engine.begin() as conn:
        for field, new_v in new_values.items():
            if field in skip_fields:
                continue
            old_v = getattr(existing, field, None)
            if old_v != new_v:
                conn.execute(force_refresh_log.insert().values(
                    partner_id=partner_id,
                    field_name=field,
                    old_value=str(old_v),
                    new_value=str(new_v),
                    reason=reason,
                    refreshed_at=_now(),
                ))
                written += 1
    return written


def persist_partner_score(
    engine: Any,
    *,
    partner_id: str,
    summary_values: dict,
    axis_scores: dict[str, Any],
) -> None:
    """Upsert partner_score_summaries (keyed on partner_id) and
    replace the per-axis scores rows for this partner.

    `axis_scores` is the CandidateScore.axis_scores dict (axis_id ->
    object with .score / .supporting_signal_ids / .confidence). Axes
    whose score is None are skipped (Stage 6's recommendation gate
    handles the missing-data case separately).

    Both writes happen inside the same transaction so a partial failure
    can't leave a partner with a fresh summary but stale per-axis rows.
    """
    with engine.begin() as conn:
        upsert(conn, partner_score_summaries, ["partner_id"], summary_values)
        conn.execute(
            delete(scores).where(scores.c.partner_id == partner_id)
        )
        for ax_id, ax_data in axis_scores.items():
            if ax_data.score is None:
                continue
            conn.execute(scores.insert().values(
                partner_id=partner_id,
                axis_id=ax_id,
                score=ax_data.score,
                supporting_signal_ids=json.dumps(ax_data.supporting_signal_ids),
                confidence=ax_data.confidence,
                scored_at=_now(),
            ))
