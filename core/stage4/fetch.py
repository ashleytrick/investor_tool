"""Stage 4 fetch + snapshot persistence (Slice 18c).

Three responsibilities:

  - upsert_snapshot(engine, source_url, text, *, final_url) -> int
        Dedup-on-(url, content_hash) write for a successful fetch.

  - upsert_snapshot_failure(engine, source_url, *, http_status, ...) -> int|None
        Audit row for a failed fetch. Returns None on UNIQUE collision.

  - _fetch_live_partner_content(ws, engine, run, known_partner_ids, ...)
        Read data/raw/partner_content_urls.csv, fetch each URL via
        http_client, return dict matching partner_signals_seed.json shape.

Lifted verbatim from scripts/04_mine_partner_signals.py; signatures
unchanged so any external caller importing these from the script keeps
working through the back-compat re-exports there.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone

from selectolax.parser import HTMLParser
from sqlalchemy import select

from core.db import source_snapshots
from core.http_client import HttpClient
from core.sources import upsert_source


STAGE = "04_mine_partner_signals"
PARTNER_CONTENT_URLS_PATH = "data/raw/partner_content_urls.csv"
CSV_REQUIRED_HEADERS = {"partner_id", "source_type", "source_url"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert_snapshot_failure(
    engine, source_url: str, *, http_status: int, final_url: str | None,
    note: str,
) -> int | None:
    chash = _content_hash(f"FAIL:{http_status}:{note}")
    with engine.begin() as conn:
        # Slice 18b: register the URL even on failure.
        sid = upsert_source(
            conn, source_url=source_url, source_type="partner_content",
        )
        try:
            result = conn.execute(source_snapshots.insert().values(
                source_url=source_url,
                source_id=sid,
                final_url=final_url,
                fetched_at=_now(),
                http_status=http_status,
                content_hash=chash,
                extracted_text=None,
                fetched_during_stage=STAGE,
            ))
            return int(result.inserted_primary_key[0])
        except Exception:  # noqa: BLE001 - UNIQUE collision on (url, hash)
            return None


def upsert_snapshot(engine, source_url: str, text: str,
                    *, final_url: str | None = None) -> int:
    """Return snapshot_id; create if (source_url, content_hash) not present."""
    chash = _content_hash(text)
    with engine.begin() as conn:
        sid = upsert_source(
            conn, source_url=source_url, source_type="partner_content",
        )
        existing = conn.execute(
            select(source_snapshots.c.snapshot_id).where(
                source_snapshots.c.source_url == source_url,
                source_snapshots.c.content_hash == chash,
            )
        ).first()
        if existing:
            return int(existing.snapshot_id)
        result = conn.execute(source_snapshots.insert().values(
            source_url=source_url,
            source_id=sid,
            final_url=final_url,
            fetched_at=_now(),
            http_status=200,
            content_hash=chash,
            extracted_text=text,
            fetched_during_stage=STAGE,
        ))
        return int(result.inserted_primary_key[0])


def _fetch_live_partner_content(
    ws, engine, run, known_partner_ids: set[str],
    *, strict_unknown_partners: bool = True,
) -> dict:
    """Read data/raw/partner_content_urls.csv (cols: partner_id, source_type,
    source_url), fetch each URL via http_client, and return a dict matching
    the partner_signals_seed.json shape so the rest of Stage 4 is identical.

    See the Stage 4 docstring (scripts/04_mine_partner_signals.py) for
    the full contract -- unknown_partner handling, CSV validation,
    fetch-failure audit, etc.
    """
    csv_path = ws.path / PARTNER_CONTENT_URLS_PATH
    if not csv_path.exists():
        return {}
    client = HttpClient()
    out: dict[str, dict] = {}
    by_partner: dict[str, list[tuple[str, str]]] = defaultdict(list)

    from core.csv_ingest import (
        CsvIngestSchema, ingest_csv, in_set, require_field,
    )

    schema = CsvIngestSchema(
        required_headers=CSV_REQUIRED_HEADERS,
        row_validators=(
            require_field("partner_id"),
            require_field("source_url"),
            in_set("partner_id", known_partner_ids,
                   error_type="unknown_partner_in_csv"),
        ),
    )
    result = ingest_csv(csv_path, schema)
    if result.missing_headers:
        msg = (
            f"partner_content_urls.csv missing required column(s): "
            f"{result.missing_headers} (have: {result.headers})"
        )
        run.log_error(str(csv_path), "csv_schema", msg)
        run.failed += 1
        raise ValueError(msg)
    for err in result.row_errors:
        run.log_error(
            err.record_id, err.error_type,
            f"row {err.row_num}: {err.message}",
        )
        if err.error_type == "unknown_partner_in_csv" and strict_unknown_partners:
            run.failed += 1
    for row in result.rows:
        pid = row["partner_id"]
        url = row["source_url"]
        stype = row.get("source_type") or "blog"
        by_partner[pid].append((stype, url))

    from core.source_fetch import fetch_and_record_sync

    for pid, items in by_partner.items():
        sources: list[dict] = []
        for stype, url in items:
            outcome = fetch_and_record_sync(
                engine, client, url, stage=STAGE,
            )
            if outcome.error or not outcome.ok:
                print(
                    f"[stage 4] {pid} {url} -> {outcome.error}; skipping"
                )
                run.log_error(
                    f"{pid}:{url}",
                    "fetch_failed" if outcome.status < 0 else "http_error",
                    outcome.error or f"HTTP {outcome.status}",
                )
                run.failed += 1
                continue
            text = HTMLParser(outcome.text).text(separator=" ", strip=True)
            if not text:
                run.log_error(
                    f"{pid}:{url}", "empty_body",
                    f"HTTP 200 but extracted text was empty "
                    f"(final_url={outcome.final_url!r})",
                )
                run.failed += 1
                continue
            sources.append({
                "source_type": stype,
                "source_url": url,
                "final_url": outcome.final_url,
                "quote_date": None,
                "text": text,
            })
        if sources:
            out[pid] = {"sources": sources}
            print(f"[stage 4] {pid}: {len(sources)} live content source(s) fetched")
    return out
