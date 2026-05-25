"""One-line startup banner so each script surfaces key state up front
instead of buried in stage chatter.

Batch 14 (#307/#308): the banner now reports which LLM model will be
used (batch vs email) and whether Stage 0 schema verification has ever
succeeded for this workspace, instead of just "ready" the moment a key
is present.
"""
from __future__ import annotations

from core.config_loader import Workspace


def print_banner(ws: Workspace, *, stage: str | None = None) -> None:
    if not ws.env("ANTHROPIC_API_KEY"):
        llm_mode = "stub"
    else:
        # Surface the actual model IDs the operator will burn budget on so
        # an accidental switch between MODEL_BATCH/MODEL_EMAIL is visible
        # at run start instead of in the bill.
        try:
            from core.llm.client import MODEL_BATCH, MODEL_EMAIL
            llm_mode = f"live({MODEL_BATCH}/{MODEL_EMAIL})"
        except Exception:  # noqa: BLE001 - fall back on import problems
            llm_mode = "live"
    if not (ws.attio or {}):
        attio_mode = "off"
    elif not ws.env("ATTIO_API_KEY"):
        attio_mode = "configured-but-no-key"
    else:
        # "ready" used to mean "config + key present" -- it didn't reflect
        # whether the schema actually matched Attio. Check the runs table
        # for a successful Stage 0 verification.
        attio_mode = _attio_state(ws)
    parts = [
        f"workspace={ws.name}",
        # Batch 30 (#528): surface the declared mode (fixture/dev/prod)
        # so the operator sees the safety state at run start.
        f"mode={getattr(ws, 'mode', 'dev')}",
        f"llm={llm_mode}",
        f"attio={attio_mode}",
    ]
    if stage:
        parts.insert(0, f"stage={stage}")
    print(f"[{' | '.join(parts)}]")


def _attio_state(ws: Workspace) -> str:
    """Read the latest 00_verify_attio_schema run from the workspace DB to
    distinguish 'key + config present' from 'verified to match Attio'."""
    try:
        from sqlalchemy import desc, select
        from core.db import get_engine, runs
        engine = get_engine(ws.db_url)
        with engine.begin() as conn:
            row = conn.execute(
                select(
                    runs.c.records_failed, runs.c.completed_at,
                ).where(runs.c.stage == "00_verify_attio_schema")
                .order_by(desc(runs.c.run_id)).limit(1)
            ).first()
    except Exception:  # noqa: BLE001 - DB may not exist yet
        return "key-set,never-verified"
    if row is None:
        return "key-set,never-verified"
    if (row.records_failed or 0) > 0:
        return "key-set,last-verify-FAILED"
    return "verified"
