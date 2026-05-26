"""Stage 4 fetch + snapshot persistence (Slice 18c).

Three responsibilities:

  - upsert_snapshot(engine, source_url, text, *, final_url) -> int
        Dedup-on-(url, content_hash) write for a successful fetch.

  - upsert_snapshot_failure(engine, source_url, *, http_status, ...) -> int|None
        Audit row for a failed fetch. Returns None on UNIQUE collision.

  - _fetch_live_partner_content(ws, engine, run, known_partner_ids, ...)
        Read data/raw/partner_content_urls.csv when present, fetch each URL via
        http_client, and supplement with Stage 2 fund pages that mention each
        partner by name. Returns a dict matching partner_signals_seed.json.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone

from selectolax.parser import HTMLParser
from sqlalchemy import select

from core.db import funds, partners, source_snapshots
from core.http_client import HttpClient
from core.ids import normalize_name
from core.sources import upsert_source


STAGE = "04_mine_partner_signals"
PARTNER_CONTENT_URLS_PATH = "data/raw/partner_content_urls.csv"
CSV_REQUIRED_HEADERS = {"partner_id", "source_type", "source_url"}
STAGE2_SOURCE_STAGE = "02_enrich_funds"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _name_in_text(name: str, text: str) -> bool:
    norm_name = normalize_name(name)
    norm_text = normalize_name(text)
    return bool(norm_name and norm_name in norm_text)


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


def _stage2_partner_sources(engine, known_partner_ids: set[str]) -> dict[str, list[dict]]:
    """Use already-fetched Stage 2 fund pages as partner content.

    If Stage 2 discovered a team/people page and the extracted page text names
    a partner, that page is useful Stage 4 evidence even when the operator has
    not supplied `partner_content_urls.csv`. This is conservative: it only
    attaches a page to a partner when the partner's normalized full name appears
    in the Stage 2 snapshot text.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    with engine.begin() as conn:
        partner_rows = list(conn.execute(
            select(
                partners.c.partner_id,
                partners.c.name,
                partners.c.fund_id,
                funds.c.domain,
            ).join(funds, funds.c.fund_id == partners.c.fund_id)
            .where(partners.c.partner_id.in_(known_partner_ids))
        ))
        snapshots = list(conn.execute(
            select(
                source_snapshots.c.source_url,
                source_snapshots.c.final_url,
                source_snapshots.c.extracted_text,
            ).where(
                source_snapshots.c.fetched_during_stage == STAGE2_SOURCE_STAGE,
                source_snapshots.c.extracted_text.isnot(None),
            )
        ))
    for p in partner_rows:
        for snap in snapshots:
            text = snap.extracted_text or ""
            if not _name_in_text(p.name, text):
                continue
            # Prefer pages from the partner's fund domain, but still accept
            # same-site redirects/canonical URLs because Stage 2 records the
            # original source_url and final_url separately.
            if p.domain and p.domain not in (snap.source_url or ""):
                final_url = snap.final_url or ""
                if p.domain not in final_url:
                    continue
            out[p.partner_id].append({
                "source_type": "fund_profile",
                "source_url": snap.source_url,
                "final_url": snap.final_url,
                "quote_date": None,
                "text": text,
            })
    return out


def _merge_partner_sources(
    out: dict[str, dict],
    partner_id: str,
    sources: list[dict],
) -> None:
    if not sources:
        return
    existing = out.setdefault(partner_id, {"sources": []})
    seen = {s.get("source_url") for s in existing["sources"]}
    for source in sources:
        if source.get("source_url") in seen:
            continue
        existing["sources"].append(source)
        seen.add(source.get("source_url"))


def _fetch_live_partner_content(
    ws, engine, run, known_partner_ids: set[str],
    *, strict_unknown_partners: bool = True,
) -> dict:
    """Fetch partner content from CSV and Stage 2 fallback snapshots.

    CSV rows remain the highest-fidelity path for partner blogs, podcasts,
    LinkedIn, Substack, etc. When no CSV exists, or when it covers only some
    partners, Stage 4 now supplements from Stage 2 fund pages that mention the
    partner by name. That lets a fund-domain-only workflow still produce basic
    partner evidence from discovered team/profile pages.
    """
    csv_path = ws.path / PARTNER_CONTENT_URLS_PATH
    client = HttpClient()
    out: dict[str, dict] = {}
    by_partner: dict[str, list[tuple[str, str]]] = defaultdict(list)

    if csv_path.exists():
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
        _merge_partner_sources(out, pid, sources)

    fallback_sources = _stage2_partner_sources(engine, known_partner_ids)
    fallback_count = 0
    for pid, sources in fallback_sources.items():
        before = len(out.get(pid, {}).get("sources", []))
        _merge_partner_sources(out, pid, sources)
        after = len(out.get(pid, {}).get("sources", []))
        fallback_count += max(0, after - before)
    if fallback_count:
        run.note(
            f"added {fallback_count} Stage 2 fund-page source(s) as "
            f"partner content fallback"
        )

    for pid, entry in out.items():
        print(
            f"[stage 4] {pid}: {len(entry.get('sources', []))} "
            f"content source(s) available"
        )
    return out
