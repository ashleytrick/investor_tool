"""Stage 2 fetch + snapshot persistence (Slice 18c).

Three responsibilities:

  - gather_fixture_pages(fund, ws) -> {url: html}
        Read local fixture HTML from `data/fixtures/fund_pages/{domain}/*.html`.
        Returns an empty dict when the fund's fixture directory doesn't
        exist (a fund seeded via funds_seed.csv but missing fixture HTML).

  - gather_live_pages(fund) -> (pages, required_failures, optional_failures)
        Fetch the homepage first, discover likely internal team / portfolio /
        thesis / news / about pages from its links, then fetch those plus the
        fixed fallback paths. Operators only need to provide the fund domain.

  - store_snapshots(engine, pages) -> int
        Dedup-on-(url, content_hash) write into source_snapshots. Each new
        snapshot also registers its URL via core.sources.upsert_source
        (Slice 18b) so the canonical sources registry stays in lockstep.

Lifted from scripts/02_enrich_funds.py; signatures unchanged so any external
caller importing these from the script keeps working through the back-compat
re-exports there.
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timezone

from selectolax.parser import HTMLParser
from sqlalchemy import select

from core.db import source_snapshots
from core.http_client import HttpClient, FetchResult
from core.sources import upsert_source
from core.stage2.discovery import discover_fund_pages


STAGE = "02_enrich_funds"

LIVE_PATHS_REQUIRED: tuple[str, ...] = ("",)
LIVE_PATHS_OPTIONAL: tuple[str, ...] = (
    "portfolio", "team", "people", "partners", "investors",
    "investment-team", "our-team", "team-members", "thesis", "approach",
    "about", "about-us", "who-we-are", "firm", "news", "insights",
)
LIVE_PATHS = list(LIVE_PATHS_REQUIRED + LIVE_PATHS_OPTIONAL)
DISCOVERED_PAGE_LIMIT = 10
LIVE_PAGE_FETCH_LIMIT = 14


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


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url.rstrip("/") or url
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def gather_fixture_pages(fund: dict, ws) -> dict[str, str]:
    """Read local fixture HTML for a fund. Returns {url: html}."""
    fx_dir = ws.fixtures_dir / "fund_pages" / fund["domain"]
    pages: dict[str, str] = {}
    if not fx_dir.is_dir():
        return pages
    for f in sorted(fx_dir.glob("*.html")):
        pages[page_url(fund["domain"], f.stem)] = f.read_text(encoding="utf-8")
    return pages


async def _fetch_page(
    client: HttpClient,
    url: str,
) -> tuple[FetchResult | None, str | None]:
    try:
        return await client.fetch(url), None
    except Exception as exc:  # noqa: BLE001 - caller buckets for audit
        return None, f"{type(exc).__name__}: {exc}"


async def gather_live_pages(
    fund: dict,
) -> tuple[dict[str, dict], list[tuple[str, str]], list[tuple[str, str]]]:
    """Fetch live fund pages with homepage-driven discovery.

    Homepage is required. Once it is fetched, Stage 2 extracts internal links
    and ranks likely team / people / portfolio / thesis / news pages, then
    fetches the best discovered URLs plus fixed fallback paths. This lets the
    operator provide only a fund domain while still finding pages like
    /people, /investment-team, /who-we-are, or /companies.
    """
    client = HttpClient()
    pages: dict[str, dict] = {}
    required_failures: list[tuple[str, str]] = []
    optional_failures: list[tuple[str, str]] = []

    homepage = page_url(fund["domain"], "")
    res, error = await _fetch_page(client, homepage)
    if error:
        required_failures.append((homepage, error))
        return pages, required_failures, optional_failures
    if res is None or res.status != 200 or not res.text.strip():
        status = res.status if res is not None else "?"
        required_failures.append((homepage, f"HTTP {status}"))
        return pages, required_failures, optional_failures

    pages[homepage] = {"html": res.text, "final_url": res.final_url}

    discovered = discover_fund_pages(
        res.final_url or homepage,
        res.text,
        max_pages=DISCOVERED_PAGE_LIMIT,
    )
    discovered_urls = [p.url for p in discovered]
    fixed_optional_urls = [page_url(fund["domain"], p) for p in LIVE_PATHS_OPTIONAL]
    candidate_urls = _dedupe_urls(discovered_urls + fixed_optional_urls)
    candidate_urls = [u for u in candidate_urls if (u.rstrip("/") or u) != homepage.rstrip("/")]
    candidate_urls = candidate_urls[:LIVE_PAGE_FETCH_LIMIT]

    if not any(p.category == "team" for p in discovered):
        optional_failures.append((
            homepage,
            "no likely team/people page discovered from homepage links; "
            "fixed fallback paths will still be tried",
        ))

    for url in candidate_urls:
        res, error = await _fetch_page(client, url)
        if error:
            optional_failures.append((url, error))
            continue
        if res is None:
            optional_failures.append((url, "no response"))
            continue
        if res.status == 200 and res.text.strip():
            pages[url] = {"html": res.text, "final_url": res.final_url}
            continue
        if res.status >= 500:
            optional_failures.append((url, f"HTTP {res.status}"))
            continue
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
