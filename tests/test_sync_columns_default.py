"""Audit-review fix: _sync_columns_with_metadata emits DEFAULT clauses.

Pre-fix, ALTER TABLE ADD COLUMN ran without a DEFAULT, so legacy
rows on upgraded DBs ended up with NULL for any newly-added
column that declared a SQLAlchemy default. (Concrete bug:
outreach_events.channel column existed but every Gmail-poll row
written before the FR-7 PR had channel=NULL, and the mark-sent
dedup path's `existing.channel or 'linkedin'` fallback then
returned 'linkedin' for what were actually email sends.)
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from sqlalchemy import Column, MetaData, Table, Text, create_engine


def test_ddl_default_clause_renders_string_default() -> None:
    from core.db import _ddl_default_clause
    md = MetaData()
    t = Table("t", md, Column("c", Text, default="email"))
    assert _ddl_default_clause(t.c.c) == " DEFAULT 'email'"


def test_ddl_default_clause_skips_callable_default() -> None:
    """Callable defaults (e.g. datetime.now) aren't portable to
    SQL; skipped silently."""
    from core.db import _ddl_default_clause
    md = MetaData()
    t = Table("t", md, Column("c", Text, default=lambda: "x"))
    assert _ddl_default_clause(t.c.c) == ""


def test_ddl_default_clause_handles_no_default() -> None:
    from core.db import _ddl_default_clause
    md = MetaData()
    t = Table("t", md, Column("c", Text))
    assert _ddl_default_clause(t.c.c) == ""


def test_ddl_default_clause_escapes_quotes() -> None:
    from core.db import _ddl_default_clause
    md = MetaData()
    t = Table("t", md, Column("c", Text, default="it's"))
    assert _ddl_default_clause(t.c.c) == " DEFAULT 'it''s'"


def test_alter_table_add_column_backfills_legacy_rows(tmp_path: Path) -> None:
    """End-to-end: create a table with one column, insert rows
    (so they're 'legacy'), then add a new column that declares a
    SQLAlchemy default. Existing rows must show the default value,
    not NULL.
    """
    db_path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO foo (id, name) VALUES (1, 'a'), (2, 'b')"
        )
    # Now simulate a new column added in the metadata...
    from sqlalchemy import Column, MetaData, Table
    md = MetaData()
    foo = Table(
        "foo", md,
        Column("id", Text, primary_key=True),
        Column("name", Text),
        Column("channel", Text, default="email"),
    )

    # ...and run the column-sync helper against this engine with
    # the metadata pointing at the new shape.
    from core.db import _sync_columns_with_metadata
    import core.db as core_db_mod
    # Swap in our local metadata for the test.
    saved = core_db_mod.metadata
    core_db_mod.metadata = md
    try:
        _sync_columns_with_metadata(eng)
    finally:
        core_db_mod.metadata = saved

    # Both legacy rows should now have channel='email', not NULL.
    with eng.begin() as conn:
        rows = list(
            conn.exec_driver_sql("SELECT id, channel FROM foo ORDER BY id")
        )
    assert rows == [(1, "email"), (2, "email")], (
        f"legacy rows should have backfilled channel='email'; got {rows}"
    )
