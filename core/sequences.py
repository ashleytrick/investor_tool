"""FR-4b: auto-stop helpers for the sequence state machine.

Background hooks (B3 reconcile-drafts, B6 poll-crm-pipeline) call
into here when they detect a signal that should halt outreach for
a partner -- a new reply, a CRM stage advance, a manual pass, a
fund news event. The helpers check the operator's cadence
preferences (`cadence_settings.auto_stop_on_*`) before mutating
so the operator can opt out per-reason.

Idempotent by design: re-stopping an already-stopped sequence is
a no-op. The first stop reason wins -- e.g. if a reply arrives
before a pipeline-advance, the sequence is stopped with
reason='reply' and a later pipeline-poll attempt does nothing.
This matches the contract for `/sequences/{id}/stop` (see
web/routers/sequences.py).
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import select

from core.db import cadence_settings, sequences


# Cadence-setting key per reason. 'user' + 'max_touches' bypass
# the gate -- those are explicit operator / system actions, not
# preference-gated background events.
_REASON_TO_SETTING_COL: dict[str, str] = {
    "reply": "auto_stop_on_reply",
    "pipeline": "auto_stop_on_pipeline_advance",
    "manual": "auto_stop_on_manual_pass",
    "fund_news": "auto_stop_on_fund_news",
}


def _auto_stop_allowed(conn, reason: str) -> bool:
    """Read the cadence_settings row and check the toggle for this
    reason. Defaults to True when the setting row hasn't been
    seeded yet -- the seed values in `web.routers.cadence` match
    these defaults, so the gate behaves the same once a settings
    row exists."""
    col = _REASON_TO_SETTING_COL.get(reason)
    if col is None:
        return True  # 'user' / 'max_touches' / unknown -> always allowed
    row = conn.execute(
        select(cadence_settings).where(
            cadence_settings.c.key == "default",
        )
    ).first()
    if row is None:
        # Seed defaults (mirrors cadence.py): reply / pipeline /
        # manual default-on; fund_news default-off.
        return col != "auto_stop_on_fund_news"
    return bool(getattr(row, col, True))


def auto_stop_sequence_if_active(
    conn,
    *,
    partner_id: str,
    reason: str,
) -> bool:
    """Flip the partner's sequence to stopped if (a) it exists,
    (b) it's currently active, and (c) the cadence setting for
    this reason permits auto-stop. Returns True if a stop
    happened, False otherwise. Idempotent -- safe to call on
    every poll pass."""
    if not _auto_stop_allowed(conn, reason):
        return False
    row = conn.execute(
        select(sequences).where(
            sequences.c.partner_id == partner_id,
        )
    ).first()
    if row is None or row.state != "active":
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    conn.execute(
        sequences.update()
        .where(sequences.c.sequence_id == row.sequence_id)
        .values(
            state="stopped",
            stopped_reason=reason,
            updated_at=now,
        )
    )
    return True
