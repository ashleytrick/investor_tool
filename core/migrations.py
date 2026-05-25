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


MIGRATIONS: list[Migration] = [
    Migration(
        id="m001_baseline",
        description=(
            "Slice 16 baseline -- post-Slice-15 schema. "
            "Records that the workspace is using the migrations system."
        ),
        apply=_m001_baseline,
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


def _workspace_predates_migrations(engine: Engine) -> bool:
    """A workspace `predates migrations` when schema_migrations doesn't
    exist AND any user table does. A truly fresh workspace (no user
    tables yet) is treated as new -- after create_all runs it'll have
    the latest schema and we just stamp all migrations as applied."""
    insp = inspect(engine)
    if insp.has_table("schema_migrations"):
        return False
    existing = set(insp.get_table_names())
    existing.discard("sqlite_sequence")  # SQLite internal
    return len(existing) > 0


def apply_pending_migrations(engine: Engine) -> list[str]:
    """Apply every migration in MIGRATIONS that isn't yet in
    schema_migrations. For brand-new workspaces (no user tables
    existed before metadata.create_all ran), stamp every migration
    as already-applied without executing -- the fresh schema IS the
    latest schema. For existing operator workspaces, run pending
    migrations in MIGRATIONS order, each in its own transaction.

    Returns the list of migration ids newly applied (or stamped) on
    this call -- empty list when nothing needed doing.
    """
    is_fresh_or_unstamped = not inspect(engine).has_table("schema_migrations")
    predates = _workspace_predates_migrations(engine)
    # Ensure the schema_migrations table exists.
    _migration_meta.create_all(engine)

    if is_fresh_or_unstamped and not predates:
        # Brand-new workspace. metadata.create_all already produced
        # the latest schema; stamp every known migration so future
        # apply_pending() calls have nothing to do.
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
