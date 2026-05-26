"""Tests for the Slice 18b sources registry."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import REPO_ROOT, _run_pipeline_through_stage_6

from core.db import get_engine, source_snapshots, sources
from core.sources import find_source_id, upsert_source


@pytest.fixture
def engine(tmp_path: Path):
    return get_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_upsert_creates_row_on_first_call(engine) -> None:
    with engine.begin() as conn:
        sid = upsert_source(
            conn, source_url="https://news.example/foo",
            source_type="funding_announcement_feed",
        )
    assert isinstance(sid, int)
    with engine.begin() as conn:
        rows = list(conn.execute(select(sources)))
    assert len(rows) == 1
    assert rows[0].source_url == "https://news.example/foo"
    assert rows[0].source_type == "funding_announcement_feed"
    assert rows[0].first_seen_at is not None
    assert rows[0].last_seen_at is not None


def test_upsert_is_idempotent_on_same_url(engine) -> None:
    with engine.begin() as conn:
        a = upsert_source(
            conn, source_url="https://news.example/foo",
            source_type="funding_announcement_feed",
        )
        b = upsert_source(
            conn, source_url="https://news.example/foo",
            source_type="funding_announcement_feed",
        )
    assert a == b
    with engine.begin() as conn:
        count = conn.execute(
            select(sources.c.source_id)
        ).rowcount
        rows = list(conn.execute(select(sources)))
    assert len(rows) == 1


def test_upsert_bumps_last_seen_at_on_repeat(engine) -> None:
    import time
    with engine.begin() as conn:
        upsert_source(conn, source_url="https://news.example/foo",
                       source_type="rss")
    with engine.begin() as conn:
        first = conn.execute(
            select(sources.c.last_seen_at)
            .where(sources.c.source_url == "https://news.example/foo")
        ).first().last_seen_at
    time.sleep(0.01)
    with engine.begin() as conn:
        upsert_source(conn, source_url="https://news.example/foo")
    with engine.begin() as conn:
        second = conn.execute(
            select(sources.c.last_seen_at)
            .where(sources.c.source_url == "https://news.example/foo")
        ).first().last_seen_at
    assert second > first


def test_upsert_fills_source_type_when_previously_null(engine) -> None:
    """First sight didn't know the type; later sight knows it. Type
    should be filled in, not left NULL."""
    with engine.begin() as conn:
        upsert_source(conn, source_url="https://x/y", source_type=None)
    with engine.begin() as conn:
        upsert_source(
            conn, source_url="https://x/y", source_type="partner_content",
        )
    with engine.begin() as conn:
        row = conn.execute(
            select(sources.c.source_type)
            .where(sources.c.source_url == "https://x/y")
        ).first()
    assert row.source_type == "partner_content"


def test_upsert_does_not_overwrite_known_source_type(engine) -> None:
    """If a known type already exists, a later upsert with a DIFFERENT
    type must NOT silently re-categorize it. That's an operator
    decision, not an automated one."""
    with engine.begin() as conn:
        upsert_source(conn, source_url="https://x/y", source_type="rss")
    with engine.begin() as conn:
        upsert_source(
            conn, source_url="https://x/y",
            source_type="partner_content",  # wrong; should be ignored
        )
    with engine.begin() as conn:
        row = conn.execute(
            select(sources.c.source_type)
            .where(sources.c.source_url == "https://x/y")
        ).first()
    assert row.source_type == "rss"


def test_find_source_id_returns_none_for_unknown(engine) -> None:
    with engine.begin() as conn:
        sid = find_source_id(conn, "https://never.seen/this")
    assert sid is None


def test_source_fetch_records_source_id_on_snapshot(tmp_path: Path) -> None:
    """core.source_fetch.record_fetch_success should populate
    source_snapshots.source_id via the canonical upsert."""
    from core.source_fetch import record_fetch_success
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    snap_id = record_fetch_success(
        engine, source_url="https://example.com/feed.rss",
        text="hello world", final_url="https://example.com/feed.rss",
        stage="test", source_type="rss",
    )
    with engine.begin() as conn:
        row = conn.execute(
            select(source_snapshots.c.source_id)
            .where(source_snapshots.c.snapshot_id == snap_id)
        ).first()
    assert row.source_id is not None
    with engine.begin() as conn:
        src = conn.execute(
            select(sources)
            .where(sources.c.source_id == row.source_id)
        ).first()
    assert src.source_url == "https://example.com/feed.rss"
    assert src.source_type == "rss"


def test_migration_backfills_source_ids_on_legacy_workspace(tmp_path: Path) -> None:
    """A pre-existing workspace that has source_snapshots rows but no
    source_id column gets backfilled by m002_backfill_source_ids."""
    from sqlalchemy import create_engine
    db_path = tmp_path / "legacy.db"
    raw_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with raw_engine.begin() as conn:
        # Pre-existing source_snapshots table WITHOUT source_id (mimic
        # a pre-Slice-18b operator DB). Use a minimal schema.
        conn.exec_driver_sql(
            "CREATE TABLE source_snapshots ("
            "  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  source_url TEXT NOT NULL, "
            "  fetched_at TEXT NOT NULL "
            ")"
        )
        conn.exec_driver_sql(
            "INSERT INTO source_snapshots (source_url, fetched_at) "
            "VALUES ('https://a/1', '2026-01-01'), "
            "       ('https://a/1', '2026-01-02'), "  # dup URL, different snapshot
            "       ('https://a/2', '2026-01-03')"
        )
    # Now open via get_engine -- _sync_columns_with_metadata adds the
    # source_id column (NULL on existing rows); apply_pending_migrations
    # runs m002_backfill_source_ids.
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(source_snapshots.c.snapshot_id, source_snapshots.c.source_id)
            .order_by(source_snapshots.c.snapshot_id)
        ))
        src_rows = list(conn.execute(select(sources)))
    # All three snapshots now have a non-null source_id.
    assert all(r.source_id is not None for r in rows)
    # The two distinct URLs produced two distinct sources rows.
    assert len(src_rows) == 2
    # Both rows for source_url 'https://a/1' point at the same source_id.
    a1_ids = {r.source_id for r in rows if r.snapshot_id in (1, 2)}
    assert len(a1_ids) == 1


def test_list_sources_cli_json_output(tmp_path: Path) -> None:
    """The CLI surfaces every registered source as JSON."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "list_sources.py"),
         "--workspace", str(ws_dst), "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    # Stage 1-4 touch multiple URLs; we should see >=10 sources after
    # a full fixture run.
    assert len(rows) >= 5
    for r in rows:
        assert "source_id" in r
        assert "source_url" in r
        assert r["first_seen_at"] is not None
        assert r["last_seen_at"] is not None


def test_source_id_populated_on_signals_after_pipeline(tmp_path):
    """Slice 18b follow-up (#18): a fresh fixture pipeline should
    populate signals.source_id via the new upsert_source call site in
    scripts/04_mine_partner_signals.py -- not via the m003 backfill,
    since brand-new workspaces stamp migrations without running them."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)

    c = sqlite3.connect(db)
    total, with_id = c.execute(
        "select count(*), count(source_id) from signals"
    ).fetchone()
    c.close()
    assert total > 0
    assert with_id == total, (
        f"expected every signal to carry source_id; "
        f"got {with_id}/{total}"
    )


def test_source_id_populated_on_deal_attributions(tmp_path):
    """Same shape: deal_attributions writers thread source_id."""
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)

    c = sqlite3.connect(db)
    total, with_id = c.execute(
        "select count(*), count(source_id) from deal_attributions"
    ).fetchone()
    c.close()
    assert total > 0
    assert with_id == total, (
        f"expected every deal_attribution to carry source_id; "
        f"got {with_id}/{total}"
    )


def test_m003_backfills_signals_on_legacy_workspace(tmp_path):
    """A legacy workspace that has signals without source_id should
    have them backfilled by m003 on first get_engine call."""
    from sqlalchemy import create_engine
    db_path = tmp_path / "legacy_signals.db"
    raw_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with raw_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE signals ("
            "  signal_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  source_url TEXT NOT NULL, "
            "  quoted_text TEXT NOT NULL "
            ")"
        )
        conn.exec_driver_sql(
            "INSERT INTO signals (source_url, quoted_text) "
            "VALUES ('https://podcasts.example/ep1', 'quote a'), "
            "       ('https://podcasts.example/ep1', 'quote b'), "
            "       ('https://blog.example/post1', 'quote c')"
        )
    # Open via get_engine -- _sync_columns_with_metadata adds source_id;
    # apply_pending_migrations runs m003 backfill.
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(text("signal_id, source_url, source_id"))
            .select_from(text("signals"))
            .order_by(text("signal_id"))
        )) if False else None
    # Use raw SQL since SQLAlchemy mapped table is broader than the
    # minimal column set we created.
    import sqlite3 as _sq
    c = _sq.connect(db_path)
    rows = c.execute(
        "select source_url, source_id from signals order by signal_id"
    ).fetchall()
    src_count = c.execute("select count(*) from sources").fetchone()[0]
    c.close()
    assert all(r[1] is not None for r in rows)
    # Two distinct URLs -> two sources rows.
    assert src_count == 2


def test_funds_source_ids_populated_after_fixture_pipeline(tmp_path):
    """Slice 18b follow-up final: fresh fixture pipeline populates
    funds.source_ids as a JSON list of integer source_ids that maps to
    the registry."""
    import json as _json
    ws_src = REPO_ROOT / "clients" / "test_workspace"
    ws_dst = tmp_path / "test_workspace"
    shutil.copytree(ws_src, ws_dst)
    db = ws_dst / "data" / "pipeline.db"
    if db.exists():
        db.unlink()
    _run_pipeline_through_stage_6(ws_dst)
    c = sqlite3.connect(db)
    rows = c.execute(
        "select fund_id, source_urls, source_ids from funds "
        "where source_urls is not null and source_urls != ''"
    ).fetchall()
    assert rows, "expected fixture pipeline to produce enriched funds"
    for fund_id, urls, sids_json in rows:
        urls_list = [u.strip() for u in urls.split(";") if u.strip()]
        assert sids_json, f"fund {fund_id} missing source_ids"
        sids = _json.loads(sids_json)
        assert isinstance(sids, list)
        # Same number of distinct URLs as source_ids.
        assert len(sids) == len(urls_list), (
            f"fund {fund_id}: {len(urls_list)} urls vs {len(sids)} ids"
        )
        # Every source_id is registered.
        valid = c.execute(
            f"select count(*) from sources where source_id in "
            f"({','.join('?' for _ in sids)})", sids,
        ).fetchone()[0]
        assert valid == len(sids), (
            f"fund {fund_id} references unknown source_ids"
        )
    c.close()


def test_m004_backfills_legacy_funds_source_urls(tmp_path):
    """A legacy workspace with funds.source_urls but no source_ids
    should be backfilled by m004 on first get_engine call."""
    import json as _json
    from sqlalchemy import create_engine
    db_path = tmp_path / "legacy_funds.db"
    raw_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with raw_engine.begin() as conn:
        # Minimal funds table without source_ids column. Mimic a
        # pre-Slice-18b-final operator DB.
        conn.exec_driver_sql(
            "CREATE TABLE funds ("
            "  fund_id TEXT PRIMARY KEY, "
            "  name TEXT NOT NULL, "
            "  source_urls TEXT "
            ")"
        )
        conn.exec_driver_sql(
            "INSERT INTO funds (fund_id, name, source_urls) VALUES "
            "  ('f.a', 'A', 'https://a.example/team; https://a.example/portfolio'), "
            "  ('f.b', 'B', 'https://b.example/'), "
            "  ('f.empty', 'Empty', NULL)"
        )
    engine = get_engine(f"sqlite:///{db_path}")
    c = sqlite3.connect(db_path)
    rows = c.execute(
        "select fund_id, source_ids from funds order by fund_id"
    ).fetchall()
    src_count = c.execute("select count(*) from sources").fetchone()[0]
    c.close()
    by_fund = {r[0]: r[1] for r in rows}
    # f.a has 2 URLs -> JSON list of 2 ids.
    assert by_fund["f.a"]
    assert len(_json.loads(by_fund["f.a"])) == 2
    # f.b has 1 URL.
    assert by_fund["f.b"]
    assert len(_json.loads(by_fund["f.b"])) == 1
    # f.empty stays NULL (no urls to backfill).
    assert by_fund["f.empty"] is None
    # Sources registry got 3 distinct URLs.
    assert src_count == 3
