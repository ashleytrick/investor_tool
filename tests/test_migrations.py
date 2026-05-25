"""Tests for the versioned migration system (Slice 16)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, select

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.db import get_engine
from core.migrations import (
    MIGRATIONS,
    Migration,
    applied_migration_ids,
    apply_pending_migrations,
    schema_migrations,
)


def test_fresh_workspace_stamps_all_migrations_without_running(tmp_path: Path) -> None:
    """A brand-new workspace gets the latest schema via
    metadata.create_all -- migrations should be stamped as
    already-applied so we don't try to e.g. drop a column that
    metadata.create_all never created."""
    db_path = tmp_path / "fresh.db"
    engine = get_engine(f"sqlite:///{db_path}")
    applied = applied_migration_ids(engine)
    assert applied == {m.id for m in MIGRATIONS}
    insp = inspect(engine)
    assert insp.has_table("schema_migrations")


def test_apply_pending_is_idempotent(tmp_path: Path) -> None:
    """Re-running on the same workspace must not duplicate-apply."""
    db_path = tmp_path / "idem.db"
    engine = get_engine(f"sqlite:///{db_path}")
    # First call (inside get_engine) stamped them. Second explicit call
    # should be a no-op.
    second = apply_pending_migrations(engine)
    assert second == []
    applied = applied_migration_ids(engine)
    assert applied == {m.id for m in MIGRATIONS}


def test_predates_workspace_actually_runs_migrations(tmp_path: Path) -> None:
    """An operator workspace that has user tables but no
    schema_migrations row should get migrations EXECUTED (not just
    stamped). Simulate by creating a partners table directly without
    going through get_engine, then calling apply_pending_migrations
    with a Migration that mutates the DB."""
    from sqlalchemy import create_engine

    db_path = tmp_path / "predates.db"
    raw_engine = create_engine(f"sqlite:///{db_path}", future=True)
    # Pre-existing user table, but no schema_migrations table.
    with raw_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE partners (partner_id TEXT PRIMARY KEY, name TEXT)"
        )

    ran: list[str] = []

    def _adds_column(conn: Any) -> None:
        conn.exec_driver_sql(
            "ALTER TABLE partners ADD COLUMN test_migration_column TEXT"
        )
        ran.append("ran")

    fake = Migration(
        id="m999_test_only",
        description="test fixture migration",
        apply=_adds_column,
    )
    MIGRATIONS.append(fake)
    try:
        newly = apply_pending_migrations(raw_engine)
        # m001_baseline + m999_test_only run on the pre-existing DB
        # because schema_migrations didn't exist.
        assert "m999_test_only" in newly
        # The migration body actually executed (added the column).
        with raw_engine.begin() as conn:
            cols = {
                row[1] for row in conn.exec_driver_sql(
                    "PRAGMA table_info(partners)"
                )
            }
        assert "test_migration_column" in cols
        assert ran == ["ran"]
    finally:
        MIGRATIONS.pop()


def test_already_applied_migration_is_skipped(tmp_path: Path) -> None:
    """When a migration's id is already in schema_migrations, its
    apply() must NOT run again on a subsequent apply_pending call."""
    db_path = tmp_path / "skip.db"
    engine = get_engine(f"sqlite:///{db_path}")

    ran: list[str] = []

    def _bomb(conn: Any) -> None:
        ran.append("ran")
        # If this ever fires for an already-stamped migration, that's
        # the bug -- a fresh workspace stamps without running.
        raise RuntimeError("migration body should not have run")

    fake = Migration(
        id="m001_baseline",  # SAME id as existing -- counts as applied
        description="re-registered baseline",
        apply=_bomb,
    )
    MIGRATIONS.append(fake)
    try:
        newly = apply_pending_migrations(engine)
        assert newly == [], "no migrations should be pending"
        assert ran == [], "already-applied migration's body must not run"
    finally:
        MIGRATIONS.pop()


def test_schema_migrations_records_applied_at(tmp_path: Path) -> None:
    """Every recorded migration has a non-null applied_at timestamp."""
    db_path = tmp_path / "ts.db"
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                schema_migrations.c.migration_id,
                schema_migrations.c.applied_at,
            )
        ))
    assert rows
    for r in rows:
        assert r.applied_at is not None
        # SQLite returns datetime objects via SQLAlchemy.
        assert isinstance(r.applied_at, datetime)


def test_pending_after_new_migration_added(tmp_path: Path) -> None:
    """When MIGRATIONS grows by one between sessions, the new id is
    pending until apply_pending_migrations runs."""
    db_path = tmp_path / "grow.db"
    engine = get_engine(f"sqlite:///{db_path}")
    before = applied_migration_ids(engine)

    def _noop(_conn: Any) -> None:
        pass

    fake = Migration(
        id="m998_added_later",
        description="new migration appended between releases",
        apply=_noop,
    )
    MIGRATIONS.append(fake)
    try:
        # Before applying, the new id is NOT in the applied set.
        assert "m998_added_later" not in applied_migration_ids(engine)
        newly = apply_pending_migrations(engine)
        assert newly == ["m998_added_later"]
        # Now it's applied.
        assert "m998_added_later" in applied_migration_ids(engine)
        # Baseline still applied (didn't get duplicated).
        assert applied_migration_ids(engine) == before | {"m998_added_later"}
    finally:
        MIGRATIONS.pop()
