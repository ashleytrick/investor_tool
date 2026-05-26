"""Stage 2 fetch + snapshot persistence (Slice 18c).

Three responsibilities:

  - gather_fixture_pages(fund, ws) -> {url: html}
        Read local fixture HTML from `data/fixtures/fund_pages/{domain}/*.html`.
        Returns an empty dict when the fund's fixture directory doesn't
        exist (a fund seeded via funds_seed.csv but missing fixture HTML).

  - gather_live_pages(fund) -> (pages, required_failures, optional_failures)
        Async HTTP fetch over the standard fund-page paths via
        core.http_client.HttpClient. Required-path failures (homepage /
        5xx) bubble up so Stage 2's caller can `run.fail` per fund; optional-
        path failures (e.g. missing /portfolio) only log.

  - store_snapshots(engine, pages) -> int
        Dedup-on-(url, content_hash) write into source_snapshots. Each new
        snapshot also registers its URL via core.sources.upsert_source
        (Slice 18b) so the canonical sources registry stays in lockstep.

Lifted verbatim from scripts/02_enrich_funds.py; signatures unchanged
so any external caller importing these from the script keeps working
through the back-compat re-exports there.
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
from datetime import datetime, timezone

from selectolax.parser import HTMLParser
from sqlalchemy import select

from core.db import source_snapshots
from core.http_client import HttpClient
from core.sources import upsert_source


STAGE = "02_enrich_funds"

LIVE_PATHS_REQUIRED: tuple[str, ...] = ("",)
LIVE_PATHS_OPTIONAL: tuple[str, ...] = (
    "portfolio", "team", "thesis", "about", "news",
)
LIVE_PATHS = list(LIVE_PATHS_REQUIRED + LIVE_PATHS_OPTIONAL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text(html: str) -> str:
    """Plain text from an HTML page; used by both store_snapshots and
    the LLM prompt builder."""
    return HTMLParser(html).text(separator=" ", strip=True)


def page_url(domain: str, path: str) -> str:
    """Compose the canonical https URL for a fund-page path. The
    empty path AND the fixture stem 'index' both resolve to the
    homepage trailing-slash form (matches the legacy behavior in
    scripts/02_enrich_funds.py)."""
    if path in ("", "index"):
        return f"https://{domain}/"
    return f"https://{domain}/{path}"


def _page_html(entry) -> str:
    """Pages may be {url: html_str} (fixture) or {url: {html, final_url}}
    (live). Normalize to the HTML string."""
    if isinstance(entry, dict):
        return entry.get("html", "")
    return entry


def gather_fixture_pages(fund: dict, ws) -> dict[str, str]:
    """Read local fixture HTML for a fund. Returns {url: html}."""
    fx_dir = ws.fixtures_dir / "fund_pages" / fund["domain"]
    pages: dict[str, str] = {}
    if not fx_dir.is_dir():
        return pages
    for f in sorted(fx_dir.glob("*.html")):
        pages[page_url(fund["domain"], f.stem)] = f.read_text(encoding="utf-8")
    return pages


async def gather_live_pages(
    fund: dict,
) -> tuple[dict[str, dict], list[tuple[str, str]], list[tuple[str, str]]]:
    """Fetch standard fund pages. Returns
    (pages_by_url, required_failures, optional_failures) where the
    failure lists are (url, reason) tuples for the operator audit trail.

    Required failures (transport / 5xx on a REQUIRED path) signal that
    the fund can't be enriched at all -- Stage 2's caller counts each
    as a per-fund run.fail so cron / wrappers notice. Optional
    failures (transport / 5xx on a NICE-TO-HAVE path) are logged for
    audit but don't bump run.failed; a missing /portfolio is normal.

    Batch 36 (#13): result dict carries final_url alongside the HTML so
    store_snapshots can persist the post-redirect URL into
    source_snapshots.final_url.
    """
    client = HttpClient()
    pages: dict[str, dict] = {}
    required_failures: list[tuple[str, str]] = []
    optional_failures: list[tuple[str, str]] = []
    for path in LIVE_PATHS:
        is_required = path in LIVE_PATHS_REQUIRED
        url = page_url(fund["domain"], path)
        try:
            res = await client.fetch(url)
        except Exception as exc:  # noqa: BLE001 - audited via failures
            bucket = required_failures if is_required else optional_failures
            bucket.append((url, f"{type(exc).__name__}: {exc}"))
            continue
        if res.status == 200 and res.text.strip():
            pages[url] = {"html": res.text, "final_url": res.final_url}
            continue
        if res.status >= 500:
            bucket = required_failures if is_required else optional_failures
            bucket.append((url, f"HTTP {res.status}"))
            continue
        if is_required and res.status >= 400:
            required_failures.append((url, f"HTTP {res.status}"))
    return pages, required_failures, optional_failures


def store_snapshots(engine, pages: dict) -> int:
    """Write each fetched page to source_snapshots, deduped on (url, hash).

    `pages` accepts either:
      - {url: html_str}  (fixture path; final_url stays NULL)
      - {url: {"html": ..., "final_url": ...}}  (live path; final_url is
        captured from the post-redirect httpx response, Batch 36 #13)

    Slice 18b: every new snapshot also registers via
    `core.sources.upsert_source` so the canonical sources registry
    stays consistent with source_snapshots.source_id.
    """
    written = 0
    with engine.begin() as conn:
        for url, entry in pages.items():
            html = _page_html(entry)
            final_url = entry.get("final_url") if isinstance(entry, dict) else None
            text = extract_text(html)
            chash = _content_hash(text)
            exists = conn.execute(
                select(source_snapshots.c.snapshot_id).where(
                    source_snapshots.c.source_url == url,
                    source_snapshots.c.content_hash == chash,
                )
            ).first()
            if exists:
                continue
            sid = upsert_source(
                conn, source_url=url, source_type="fund_team_page",
            )
            conn.execute(source_snapshots.insert().values(
                source_url=url,
                source_id=sid,
                final_url=final_url,
                fetched_at=_now(),
                http_status=200,
                content_hash=chash,
                extracted_text=text,
                fetched_during_stage=STAGE,
            ))
            written += 1
    return written
