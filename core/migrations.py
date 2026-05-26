"""Versioned schema migrations (Slice 16 / REFACTOR_PLAN item 9).

The existing `_sync_columns_with_metadata` in `core/db.py` handles the
easy case (new columns appended to a table) by reading the SQLAlchemy
metadata at engine-open time and ALTER-ing missing columns. It can't
handle anything richer: renames, drops, type changes, data backfills,
multi-step refactors, dropping an index, adding a UNIQUE constraint
to existing data.

This module adds the missing layer without throwing out the
convenient part. The flow at `get_engine()` time is now:

  1. `metadata.create_all(engine)` -- creates tables that don't yet
     exist (no-op for existing).
  2. `_sync_columns_with_metadata(engine)` -- additive column drift.
  3. `apply_pending_migrations(engine)` -- everything else: runs any
     migration in MIGRATIONS not yet recorded in `schema_migrations`.

Brand-new workspaces never run migrations -- `metadata.create_all`
already gave them the latest schema. The migration log is stamped
with every known migration id so future versions know there's
nothing pending. Only operator DBs that pre-date the migration
landing get migrations actually executed.

Adding a migration
------------------

Append a `Migration(...)` entry to MIGRATIONS. The `id` must be
unique and lexicographically sortable (use the `mNNN_short_name`
convention). The `apply` callable receives an open SQLAlchemy
connection inside a transaction; raise any exception to roll back
+ leave the migration unapplied.

    def _m003_add_foo_index(conn):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_foo_bar ON foo(bar)"
        )

    MIGRATIONS.append(Migration(
        id="m003_add_foo_index",
        description="Speed up the foo.bar filter Stage 9 added",
        apply=_m003_add_foo_index,
    ))
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    inspect,
    select,
)
from sqlalchemy.engine import Engine


# Use a separate MetaData so this table's schema doesn't get bundled
# into core.db.metadata.create_all (we manage it ourselves).
_migration_meta = MetaData()

schema_migrations = Table(
    "schema_migrations", _migration_meta,
    Column("migration_id", Text, primary_key=True),
    Column("applied_at", DateTime, nullable=False),
)


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    apply: Callable[[Any], None]


def _m001_baseline(_conn: Any) -> None:
    """Stamp the schema as of Slice 16. No-op apply -- the columns +
    tables this baseline covers are all created by metadata.create_all
    and _sync_columns_with_metadata. This entry exists so the
    migration log has a known starting point + future migrations have
    a parent to chain after."""


def _m002_backfill_source_ids(conn: Any) -> None:
    """Slice 18b: backfill source_snapshots.source_id by upserting
    every distinct source_url into the new `sources` registry.

    Safe to run on a workspace where some snapshots already carry a
    source_id (e.g. partial migration mid-flight): the WHERE clause
    filters to snapshots that still need a backfill, and upsert_source
    is idempotent on the URL UNIQUE index.

    Also safe to run on a workspace that doesn't have source_snapshots
    yet -- the early return below short-circuits.
    """
    # Defensive: a minimal pre-existing workspace might not have a
    # source_snapshots table yet (the test fixture for
    # `test_predates_workspace_actually_runs_migrations` is exactly
    # this shape). No table -> nothing to backfill.
    has_table = [
        row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='source_snapshots'"
        )
    ]
    if not has_table:
        return
    urls = [
        row[0] for row in conn.exec_driver_sql(
            "SELECT DISTINCT source_url FROM source_snapshots "
            "WHERE source_id IS NULL AND source_url IS NOT NULL"
        )
    ]
    if not urls:
        return
    # Import here so this module doesn't pull core.sources at top
    # level (avoids the cycle db -> migrations -> sources -> db).
    from core.sources import upsert_source
    for url in urls:
        sid = upsert_source(conn, source_url=url, source_type=None)
        conn.exec_driver_sql(
            "UPDATE source_snapshots SET source_id = ? "
            "WHERE source_url = ? AND source_id IS NULL",
            (sid, url),
        )


def _m003_backfill_source_ids_extended(conn: Any) -> None:
    """Slice 18b follow-up (#18): backfill source_id on signals,
    deal_attributions, and ambiguous_matches. Same pattern as m002 but
    across the three remaining tables that carry loose source_url.

    Each table's source_url either matches a row already in the
    `sources` registry (registered by m002 from source_snapshots, or
    by a new writer post-Slice-18b) OR is a URL we haven't seen
    elsewhere -- in which case upsert_source registers it now.

    Idempotent: re-runs no-op once source_id is populated.
    """
    from core.sources import upsert_source

    targets = (
        ("signals", "source_url"),
        ("deal_attributions", "source_url"),
        ("ambiguous_matches", "source_url"),
    )
    for table_name, url_col in targets:
        # Defensive: skip when the table doesn't exist (minimal
        # pre-existing workspaces from the test fixtures).
        has_table = [
            row[0] for row in conn.exec_driver_sql(
                f"SELECT name FROM sqlite_master "
                f"WHERE type='table' AND name='{table_name}'"
            )
        ]
        if not has_table:
            continue
        # Check the table has a source_id column (the ALTER ADD COLUMN
        # ran via _sync_columns_with_metadata before us; defensive in
        # case migration ordering changes).
        cols = {
            row[1] for row in conn.exec_driver_sql(
                f"PRAGMA table_info({table_name})"
            )
        }
        if "source_id" not in cols:
            continue
        urls = [
            row[0] for row in conn.exec_driver_sql(
                f"SELECT DISTINCT {url_col} FROM {table_name} "
                f"WHERE source_id IS NULL AND {url_col} IS NOT NULL"
            )
        ]
        for url in urls:
            sid = upsert_source(conn, source_url=url, source_type=None)
            conn.exec_driver_sql(
                f"UPDATE {table_name} SET source_id = ? "
                f"WHERE {url_col} = ? AND source_id IS NULL",
                (sid, url),
            )


def _m004_backfill_funds_source_ids(conn: Any) -> None:
    """Slice 18b follow-up (#18, final): backfill funds.source_ids
    from the legacy `; `-delimited source_urls string. For each fund
    that has a non-empty source_urls but no source_ids JSON, split on
    `; `, upsert each URL into the sources registry, write the JSON
    list of source_id values.

    Safe to run on workspaces that don't have the funds.source_ids
    column yet (defensive table+column check). Idempotent: skip funds
    whose source_ids is already set.
    """
    import json as _json

    has_table = [
        row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='funds'"
        )
    ]
    if not has_table:
        return
    cols = {
        row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(funds)"
        )
    }
    if "source_ids" not in cols or "source_urls" not in cols:
        return
    rows = list(conn.exec_driver_sql(
        "SELECT fund_id, source_urls FROM funds "
        "WHERE source_urls IS NOT NULL AND source_urls != '' "
        "  AND (source_ids IS NULL OR source_ids = '')"
    ))
    if not rows:
        return
    from core.sources import upsert_source
    for fund_id, urls_blob in rows:
        urls = [u.strip() for u in str(urls_blob).split(";") if u.strip()]
        if not urls:
            continue
        sids: list[int] = []
        for url in urls:
            sid = upsert_source(
                conn, source_url=url, source_type="fund_team_page",
            )
            if sid not in sids:
                sids.append(sid)
        conn.exec_driver_sql(
            "UPDATE funds SET source_ids = ? WHERE fund_id = ?",
            (_json.dumps(sids), fund_id),
        )


MIGRATIONS: list[Migration] = [
    Migration(
        id="m001_baseline",
        description=(
            "Slice 16 baseline -- post-Slice-15 schema. "
            "Records that the workspace is using the migrations system."
        ),
        apply=_m001_baseline,
    ),
    Migration(
        id="m002_backfill_source_ids",
        description=(
            "Slice 18b -- upsert every distinct source_snapshots.source_url "
            "into the new `sources` registry + stamp source_id on the "
            "snapshot rows. Idempotent."
        ),
        apply=_m002_backfill_source_ids,
    ),
    Migration(
        id="m003_backfill_source_ids_extended",
        description=(
            "Slice 18b follow-up (#18) -- backfill source_id on signals, "
            "deal_attributions, and ambiguous_matches. Idempotent."
        ),
        apply=_m003_backfill_source_ids_extended,
    ),
    Migration(
        id="m004_backfill_funds_source_ids",
        description=(
            "Slice 18b follow-up (#18 final) -- backfill "
            "funds.source_ids JSON list from the legacy "
            "`; `-delimited source_urls string. Idempotent."
        ),
        apply=_m004_backfill_funds_source_ids,
    ),
]


def applied_migration_ids(engine: Engine) -> set[str]:
    """Return the set of migration ids already recorded in
    schema_migrations. Returns an empty set when the table doesn't
    exist yet (workspace pre-dates this module)."""
    insp = inspect(engine)
    if not insp.has_table("schema_migrations"):
        return set()
    with engine.begin() as conn:
        return {
            row.migration_id
            for row in conn.execute(select(schema_migrations.c.migration_id))
        }


def apply_pending_migrations(
    engine: Engine,
    *,
    was_empty_before_create_all: bool | None = None,
) -> list[str]:
    """Apply every migration in MIGRATIONS that isn't yet in
    schema_migrations.

    `was_empty_before_create_all` -- callers that own the
    metadata.create_all() invocation should pass True when the DB
    had ZERO user tables at engine-open time. That tells us the
    fresh schema came from create_all, so e.g. a future
    "drop column X" migration must be STAMPED (not executed) --
    create_all never gave the brand-new DB column X to drop.
    Caller-supplied because once create_all has run, this module
    can't tell brand-new from pre-existing by inspecting the DB.

    When the flag is None (legacy callers), we fall back to a
    best-effort check: if schema_migrations doesn't exist AND no
    user tables exist either, treat as fresh; otherwise run.

    For pre-existing operator workspaces, run pending migrations
    in MIGRATIONS order, each in its own transaction.

    Returns the list of migration ids newly applied (or stamped) on
    this call -- empty list when nothing needed doing.
    """
    insp = inspect(engine)
    stamps_exist = insp.has_table("schema_migrations")

    if was_empty_before_create_all is None:
        # Legacy / direct callers: best-effort. A workspace with NO
        # user tables and NO schema_migrations is fresh; anything
        # else is treated as pre-existing.
        if not stamps_exist:
            existing = set(insp.get_table_names())
            existing.discard("sqlite_sequence")
            was_empty_before_create_all = not existing
        else:
            was_empty_before_create_all = False

    # Ensure the schema_migrations table exists.
    _migration_meta.create_all(engine)

    if was_empty_before_create_all and not stamps_exist:
        # Brand-new workspace. metadata.create_all already produced
        # the latest schema; stamp every known migration so future
        # apply_pending() calls have nothing to do AND any future
        # migration whose apply() is destructive (drop column, etc.)
        # doesn't fire against a schema that never had the target.
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            for m in MIGRATIONS:
                conn.execute(schema_migrations.insert().values(
                    migration_id=m.id,
                    applied_at=now,
                ))
        return [m.id for m in MIGRATIONS]

    # Existing operator workspace OR a workspace that already had
    # some migrations applied. Run any pending ones in order.
    applied = applied_migration_ids(engine)
    newly: list[str] = []
    for m in MIGRATIONS:
        if m.id in applied:
            continue
        with engine.begin() as conn:
            m.apply(conn)
            conn.execute(schema_migrations.insert().values(
                migration_id=m.id,
                applied_at=datetime.now(timezone.utc),
            ))
        newly.append(m.id)
    return newly
