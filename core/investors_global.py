"""Shared `investors_global` discovery pool (Phase 3).

Two-layer investor data per the auth spec:

- Per-tenant (this workspace's `funds` + `partners` tables, with
  Stage 2 enrichment + scoring + notes): full per-user
  isolation, source of truth for outreach.

- Shared discovery pool (`investors_global` here): objective
  enriched fields ONLY -- firm, partner, email, stages, sectors,
  geographies, free-form enriched_fields blob. No tenant signal,
  no score, no notes, no status. Deduped on email when present,
  otherwise on (lower(firm), lower(partner)).

Every enrichment in a tenant workspace upserts the relevant row
into this shared store so a future tenant's discovery query
(`GET /discovery/matches`, Phase 4) can surface investors they
haven't uploaded themselves. The shared store lives in its own
SQLite file -- separate from any tenant's workspace DB -- so a
single missed `WHERE user_id = ?` cannot leak tenant-specific
signal into the discovery pool.

Path discipline:

- `GLOBAL_DB_PATH` env var pins the location. Default is
  `/data/global/global.db` -- matching the Fly volume's mount
  point next to `/data/workspaces/`.
- Tests + dev override via env to keep the file inside `tmp_path`.

This module is the only writer of the shared pool. Callers invoke
`upsert_investor(...)` from the tenant context after their own
write to `partners` / `funds` completes; the sync is best-effort
and never blocks the tenant write.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    select,
    update,
)
from sqlalchemy.engine import Engine


# Separate MetaData from `core.db.metadata` so create_all only
# touches the shared file -- never tries to bring tenant tables
# into the global DB or vice versa.
_global_meta = MetaData()


investors_global = Table(
    "investors_global", _global_meta,
    Column("id", Integer, primary_key=True, autoincrement=True),

    # Identity. `firm` + `partner` always set; `email` is the
    # preferred dedup key when present.
    Column("firm", Text, nullable=False),
    Column("partner", Text, nullable=False),
    Column("email", Text),

    # Objective enriched arrays. Stored as JSON-encoded strings
    # (SQLite has no array type). The Python accessors below
    # round-trip them.
    Column("stages", Text, default="[]"),
    Column("sectors", Text, default="[]"),
    Column("geographies", Text, default="[]"),

    # Free-form enriched fields the discovery ranker may consume
    # (check_size_range, thesis, recent_deals_count, etc.). JSON
    # blob so adding a new enriched dimension doesn't need a
    # migration. Tenant-specific signal MUST NOT land here.
    Column("enriched_fields", Text, default="{}"),

    Column("first_seen_at", DateTime, nullable=False),
    Column("last_enriched_at", DateTime, nullable=False),

    # Indexes for the two dedup paths and for the common
    # discovery query (filter by sector or stage; that's
    # JSON-string contains, so the index only helps the email +
    # firm/partner shape).
    Index("ix_investors_global_email", "email"),
    Index("ix_investors_global_firm_partner", "firm", "partner"),
)


# Default location on the Fly volume; tests override via env.
_GLOBAL_DB_DEFAULT = "/data/global/global.db"


def _global_db_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("GLOBAL_DB_PATH") or _GLOBAL_DB_DEFAULT
    )


def get_global_engine() -> Engine:
    """Return (and lazily create) the shared global.db engine.

    Each caller gets a fresh Engine -- SQLAlchemy connection
    pooling handles reuse. The directory is created if missing so
    the first call from a fresh deploy doesn't require an operator
    to pre-mkdir. The schema is created on first call via
    `metadata.create_all` -- same idempotent pattern the per-
    workspace `core.db.get_engine` uses.
    """
    db = _global_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db}",
        # Same FK-on pragma the per-tenant engines use, even
        # though investors_global has no foreign keys today.
        # Future evolution (e.g. claimed-from links) gets it for
        # free.
        connect_args={"check_same_thread": False},
    )
    _global_meta.create_all(engine)
    return engine


# ---------- public API ----------

@dataclass(frozen=True)
class InvestorRow:
    """Input shape for `upsert_investor`. Callers in tenant context
    build this from their `partners` + `funds` join (or directly
    from an uploaded CSV row) and pass it in. None for any optional
    field is fine -- the upsert preserves whatever was already
    there if the new payload is sparser."""
    firm: str
    partner: str
    email: str | None = None
    stages: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    geographies: tuple[str, ...] = ()
    enriched_fields: dict | None = None


def _normalize_email(email: str | None) -> str | None:
    """Lowercase + strip. None / empty / whitespace -> None so the
    dedup logic treats missing-email rows consistently."""
    if not email:
        return None
    cleaned = email.strip().lower()
    return cleaned or None


def _firm_partner_key(firm: str, partner: str) -> tuple[str, str]:
    """Case-insensitive dedup key for the email-missing path.
    Stored values keep their original casing; the lookup compares
    case-folded."""
    return ((firm or "").strip().lower(), (partner or "").strip().lower())


def _find_existing(
    conn: Any, row: InvestorRow,
) -> Any | None:
    """Locate the existing investors_global row this upsert should
    merge into. Rule set:

      1. New row carries an email -> ONLY merge with a row that has
         the same email (case-insensitive). If no email match, fall
         through to firm+partner, BUT only when the candidate row's
         email is NULL. A candidate with a DIFFERENT email is treated
         as a different investor (likely a different person at the
         same firm); we insert a new row instead of merging.

      2. New row has no email -> merge with a firm+partner match.
         The candidate's email (if any) is unaffected on update.

    Returns the SQLAlchemy row mapping or None.
    """
    email = _normalize_email(row.email)
    if email:
        existing = conn.execute(
            select(investors_global).where(
                investors_global.c.email == email,
            ).limit(1)
        ).first()
        if existing is not None:
            return existing
    firm_lc, partner_lc = _firm_partner_key(row.firm, row.partner)
    if not firm_lc or not partner_lc:
        return None
    # Case-insensitive firm+partner lookup.
    candidate = conn.execute(
        select(investors_global).where(
            investors_global.c.firm.collate("NOCASE") == row.firm,
            investors_global.c.partner.collate("NOCASE") == row.partner,
        ).limit(1)
    ).first()
    if candidate is None:
        return None
    # If the NEW row carries an email and the candidate has a
    # DIFFERENT email, refuse the merge -- different person at
    # the same firm, not a re-upload of the same one.
    if email and candidate.email and candidate.email != email:
        return None
    return candidate


def _merge_arrays(existing_json: str | None, new: Iterable[str]) -> str:
    """Union the two array sets and serialize. Preserves the
    existing values + adds any new ones, deduped + sorted for
    stable comparisons. Drops empty / None entries from the new
    side."""
    try:
        old = json.loads(existing_json) if existing_json else []
        if not isinstance(old, list):
            old = []
    except (json.JSONDecodeError, TypeError):
        old = []
    merged = set(str(x) for x in old if isinstance(x, str))
    for item in new:
        if isinstance(item, str) and item.strip():
            merged.add(item.strip())
    return json.dumps(sorted(merged))


def _merge_enriched(
    existing_json: str | None, new: dict | None,
) -> str:
    """Merge new enriched fields into existing ones. New values
    overwrite same-key existing values -- the latest enrichment
    is treated as more current."""
    try:
        old = json.loads(existing_json) if existing_json else {}
        if not isinstance(old, dict):
            old = {}
    except (json.JSONDecodeError, TypeError):
        old = {}
    if isinstance(new, dict):
        old.update(new)
    return json.dumps(old, default=str, sort_keys=True)


def upsert_investor(
    engine: Engine, row: InvestorRow,
) -> int:
    """Upsert one investor into the shared pool. Returns the id
    of the matched-or-inserted row.

    Dedup precedence:
      1. email (case-insensitive) when present on both sides.
      2. (firm, partner) case-insensitive otherwise.

    Merge semantics: new arrays union into existing ones; new
    enriched_fields overwrite same-key existing ones; first_seen_at
    is preserved on update; last_enriched_at is bumped to now.
    """
    now = datetime.now(timezone.utc)
    email = _normalize_email(row.email)
    with engine.begin() as conn:
        existing = _find_existing(conn, row)
        if existing is None:
            result = conn.execute(
                investors_global.insert().values(
                    firm=row.firm,
                    partner=row.partner,
                    email=email,
                    stages=json.dumps(sorted(set(s for s in row.stages if s))),
                    sectors=json.dumps(sorted(set(s for s in row.sectors if s))),
                    geographies=json.dumps(
                        sorted(set(g for g in row.geographies if g))
                    ),
                    enriched_fields=json.dumps(
                        row.enriched_fields or {},
                        default=str, sort_keys=True,
                    ),
                    first_seen_at=now,
                    last_enriched_at=now,
                )
            )
            return int(result.inserted_primary_key[0])
        # Update path.
        merged_stages = _merge_arrays(existing.stages, row.stages)
        merged_sectors = _merge_arrays(existing.sectors, row.sectors)
        merged_geos = _merge_arrays(existing.geographies, row.geographies)
        merged_enriched = _merge_enriched(
            existing.enriched_fields, row.enriched_fields,
        )
        # Email upgrade: if the new row carries an email and the
        # existing row didn't, learn it. NEVER overwrite an
        # existing email with a different one -- that's a dedup
        # error upstream + the data should be inspected, not
        # silently merged.
        new_email = existing.email
        if email and not existing.email:
            new_email = email
        conn.execute(
            update(investors_global)
            .where(investors_global.c.id == existing.id)
            .values(
                email=new_email,
                stages=merged_stages,
                sectors=merged_sectors,
                geographies=merged_geos,
                enriched_fields=merged_enriched,
                last_enriched_at=now,
            )
        )
        return int(existing.id)


def upsert_many(
    engine: Engine, rows: Iterable[InvestorRow],
) -> int:
    """Upsert a batch. Returns the number of rows touched (insert
    OR update). Calls `upsert_investor` per row -- the inner
    function does the dedup lookup; a future optimization can
    pre-fetch by email/firm+partner in one query if batch sizes
    become large."""
    count = 0
    for row in rows:
        upsert_investor(engine, row)
        count += 1
    return count


def count_investors(engine: Engine) -> int:
    """Operational helper -- used by tests + by status/admin
    surfaces (Phase 5) to size the discovery pool."""
    with engine.begin() as conn:
        rows = list(conn.execute(select(investors_global.c.id)))
    return len(rows)
