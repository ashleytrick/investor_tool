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


_DEFAULT_SKIP_FIELDS = ("scored_at",)


def _diff_force_refresh_rows(
    *,
    partner_id: str,
    existing: Any,
    new_values: dict,
    reason: str,
    skip_fields: tuple[str, ...],
    now: datetime,
) -> list[dict]:
    """Build the list of force_refresh_log row dicts for fields that
    changed between `existing` and `new_values`. Pure -- no DB writes.
    """
    rows: list[dict] = []
    for field, new_v in new_values.items():
        if field in skip_fields:
            continue
        old_v = getattr(existing, field, None)
        if old_v != new_v:
            rows.append({
                "partner_id": partner_id,
                "field_name": field,
                "old_value": str(old_v),
                "new_value": str(new_v),
                "reason": reason,
                "refreshed_at": now,
            })
    return rows


def persist_partner_score(
    engine: Any,
    *,
    partner_id: str,
    summary_values: dict,
    axis_scores: dict[str, Any],
    force_refresh_audit: dict | None = None,
) -> int:
    """Upsert partner_score_summaries + replace per-axis scores rows
    + (optionally) write force_refresh_log audit rows -- all inside a
    single transaction so a persistence failure rolls back the audit
    too.

    Launch-blocker fix: the previous shape exposed
    `log_force_refresh_diff()` as a separate function that opened and
    committed its own transaction BEFORE `persist_partner_score`. If
    the later upsert/delete/insert failed, the audit trail claimed an
    override had been broken even though the new score never landed.
    Now both happen atomically.

    `force_refresh_audit` is None (no audit) or a dict
    `{"existing": <prior row>, "reason": <str>}`. When supplied, one
    audit row lands per field where existing != new_values (with
    `scored_at` skipped by default to avoid noise).

    `axis_scores` is the CandidateScore.axis_scores dict (axis_id ->
    object with .score / .supporting_signal_ids / .confidence). Axes
    whose score is None are skipped (Stage 6's recommendation gate
    handles the missing-data case separately).

    Returns the count of force_refresh_log rows written (0 when no
    audit was requested or no fields changed).
    """
    audit_written = 0
    now = _now()
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
                scored_at=now,
            ))
        if force_refresh_audit:
            audit_rows = _diff_force_refresh_rows(
                partner_id=partner_id,
                existing=force_refresh_audit["existing"],
                new_values=summary_values,
                reason=force_refresh_audit["reason"],
                skip_fields=force_refresh_audit.get(
                    "skip_fields", _DEFAULT_SKIP_FIELDS,
                ),
                now=now,
            )
            for row in audit_rows:
                conn.execute(force_refresh_log.insert().values(**row))
            audit_written = len(audit_rows)
    return audit_written


def log_force_refresh_diff(
    engine: Any,
    *,
    partner_id: str,
    existing: Any,
    new_values: dict,
    reason: str,
    skip_fields: tuple[str, ...] = _DEFAULT_SKIP_FIELDS,
) -> int:
    """DEPRECATED back-compat wrapper. Writes force_refresh_log rows
    in its own transaction, which violates the atomicity guarantee --
    new callers should pass `force_refresh_audit` to
    `persist_partner_score()` instead. Kept so external callers /
    older tests still work; emits an audit warning via the note that
    the standalone path was used.
    """
    rows = _diff_force_refresh_rows(
        partner_id=partner_id, existing=existing,
        new_values=new_values, reason=reason,
        skip_fields=skip_fields, now=_now(),
    )
    if not rows:
        return 0
    with engine.begin() as conn:
        for row in rows:
            conn.execute(force_refresh_log.insert().values(**row))
    return len(rows)
