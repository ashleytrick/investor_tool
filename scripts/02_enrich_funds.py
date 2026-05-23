"""Stage 2: enrich each fund by scraping its website.

For every fund with a domain, fetches homepage / portfolio / team / thesis /
about / news, stores each fetched page in `source_snapshots` (content-hash
deduped), runs LLM enrichment (prompts/enrich_fund.txt) validated against
schemas/fund_enrichment.py, updates the `funds` row, and discovers partners
from the team page with deterministic partner_id slugs.

Fixture mode (--fixtures): pages are read from
data/fixtures/fund_pages/{domain}/*.html instead of the network, and when no
ANTHROPIC_API_KEY is set the LLM step falls back to a deterministic extractor
over the fixture HTML so the slice runs fully offline.

Run: uv run scripts/02_enrich_funds.py --workspace clients/test_workspace --fixtures
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from selectolax.parser import HTMLParser
from sqlalchemy import select

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import funds, get_engine, partners, source_snapshots, upsert
from core.http_client import HttpClient
from core.ids import partner_id_for
from core.llm.client import MODEL_BATCH, LLMClient
from core.runs import RunLogger
from schemas.fund_enrichment import FundEnrichment

STAGE = "02_enrich_funds"
LIVE_PATHS = ["", "portfolio", "team", "thesis", "about", "news"]
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "enrich_fund.txt"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_text(html: str) -> str:
    return HTMLParser(html).text(separator=" ", strip=True)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _page_url(domain: str, path: str) -> str:
    return f"https://{domain}/" if path in ("", "index") else f"https://{domain}/{path}"


def gather_fixture_pages(fund: dict, ws) -> dict[str, str]:
    """Read local fixture HTML for a fund. Returns {url: html}."""
    fx_dir = ws.fixtures_dir / "fund_pages" / fund["domain"]
    pages: dict[str, str] = {}
    if not fx_dir.is_dir():
        return pages
    for f in sorted(fx_dir.glob("*.html")):
        pages[_page_url(fund["domain"], f.stem)] = f.read_text(encoding="utf-8")
    return pages


async def gather_live_pages(fund: dict) -> dict[str, str]:
    """Fetch standard fund pages over the network. Returns {url: html}."""
    client = HttpClient()
    pages: dict[str, str] = {}
    for path in LIVE_PATHS:
        url = _page_url(fund["domain"], path)
        try:
            res = await client.fetch(url)
            if res.status == 200 and res.text.strip():
                pages[url] = res.text
        except Exception:  # noqa: BLE001 - a missing page is not fatal
            continue
    return pages


def deterministic_enrichment(pages: dict[str, str]) -> dict:
    """Offline stub: extract enrichment from the structured fixture HTML.

    Designed for the fixture HTML format (meta tags + .partner / .portfolio-company
    nodes). The live LLM path handles arbitrary real fund sites.
    """
    out = {
        "thesis_summary": None,
        "stated_sectors": [],
        "stated_stage_focus": None,
        "check_size_range": None,
        "portfolio_companies": [],
        "current_partners": [],
        "recent_focus_signals": None,
        "explicit_kill_signals": [],
        "source_urls_used": sorted(pages.keys()),
    }
    for html in pages.values():
        tree = HTMLParser(html)

        def meta(name: str) -> str | None:
            node = tree.css_first(f'meta[name="{name}"]')
            return node.attributes.get("content") if node else None

        if (t := meta("thesis")) and not out["thesis_summary"]:
            out["thesis_summary"] = t
        if (s := meta("stage")) and not out["stated_stage_focus"]:
            out["stated_stage_focus"] = s
        if (c := meta("check-size")) and not out["check_size_range"]:
            out["check_size_range"] = c
        if (sec := meta("sectors")) and not out["stated_sectors"]:
            out["stated_sectors"] = [x.strip() for x in sec.split(",") if x.strip()]
        if (rf := meta("recent-focus")) and not out["recent_focus_signals"]:
            out["recent_focus_signals"] = rf
        if (ks := meta("kill-signal")) and ks not in out["explicit_kill_signals"]:
            out["explicit_kill_signals"].append(ks)

        for node in tree.css("div.partner"):
            name = node.attributes.get("data-name")
            if not name:
                continue
            out["current_partners"].append({
                "name": name,
                "title": node.attributes.get("data-title"),
                "bio_snippet": node.text(separator=" ", strip=True) or None,
            })
        for node in tree.css("li.portfolio-company"):
            company = node.text(strip=True)
            if company and company not in out["portfolio_companies"]:
                out["portfolio_companies"].append(company)
    return out


def enrich(llm: LLMClient, fund: dict, pages: dict[str, str]) -> FundEnrichment:
    """Run enrichment. Live: LLM over fetched content. Stub: deterministic."""
    content = "\n\n".join(
        f"--- {url} ---\n{_extract_text(html)}" for url, html in pages.items()
    )
    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{FUND_NAME}", fund["name"])
        .replace("{DOMAIN}", fund["domain"])
        .replace("{CONTENT}", content)
    )
    return llm.complete_json(
        prompt=prompt,
        schema=FundEnrichment,
        model=MODEL_BATCH,
        stub_response=deterministic_enrichment(pages),
    )


def store_snapshots(engine, pages: dict[str, str]) -> int:
    """Write each fetched page to source_snapshots, deduped on (url, hash)."""
    written = 0
    with engine.begin() as conn:
        for url, html in pages.items():
            text = _extract_text(html)
            chash = _content_hash(text)
            exists = conn.execute(
                select(source_snapshots.c.snapshot_id).where(
                    source_snapshots.c.source_url == url,
                    source_snapshots.c.content_hash == chash,
                )
            ).first()
            if exists:
                continue
            conn.execute(source_snapshots.insert().values(
                source_url=url,
                fetched_at=_now(),
                http_status=200,
                content_hash=chash,
                extracted_text=text,
                fetched_during_stage=STAGE,
            ))
            written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 2 fund enrichment.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--fixtures", action="store_true",
        help="Read pages from data/fixtures/fund_pages instead of the network.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)

    with engine.begin() as conn:
        fund_rows = [
            dict(r._mapping) for r in conn.execute(
                select(funds).where(funds.c.domain.isnot(None))
            )
        ]

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        for fund in fund_rows:
            run.processed += 1
            try:
                if args.fixtures:
                    pages = gather_fixture_pages(fund, ws)
                else:
                    pages = asyncio.run(gather_live_pages(fund))
                if not pages:
                    run.skipped += 1
                    run.log_error(fund["fund_id"], "no_pages",
                                  "no pages fetched for fund")
                    continue

                snaps = store_snapshots(engine, pages)
                enrichment = enrich(llm, fund, pages)

                with engine.begin() as conn:
                    conn.execute(
                        funds.update().where(funds.c.fund_id == fund["fund_id"]).values(
                            stated_thesis=enrichment.thesis_summary,
                            stated_stage_focus=enrichment.stated_stage_focus,
                            check_size_range=enrichment.check_size_range,
                            kill_signals="; ".join(enrichment.explicit_kill_signals)
                            or None,
                            source_urls="; ".join(
                                str(u) for u in enrichment.source_urls_used),
                            last_updated=_now(),
                        )
                    )
                    for p in enrichment.current_partners:
                        pid = partner_id_for(fund["domain"], p.name)
                        # Team page is a single-source recent observation, so
                        # this counts as "likely_current" per the brief's
                        # employment confidence ladder. The other states need
                        # additional sources that this v1 does not ingest:
                        #   verified_current = /team + LinkedIn within 30 days
                        #   uncertain        = stale or conflicting evidence
                        #   left_fund        = explicit departure evidence
                        # Until LinkedIn cross-check + departure feed are added,
                        # only likely_current is reachable from this stage.
                        upsert(conn, partners, ["partner_id"], {
                            "partner_id": pid,
                            "fund_id": fund["fund_id"],
                            "name": p.name,
                            "title": p.title,
                            "bio": p.bio_snippet,
                            "employment_status": "likely_current",
                            "last_updated": _now(),
                        })
                run.succeeded += 1
                print(
                    f"[stage 2] {fund['name']}: {len(pages)} pages "
                    f"({snaps} new snapshots), "
                    f"{len(enrichment.current_partners)} partners, "
                    f"stage={enrichment.stated_stage_focus}"
                )
            except Exception as exc:  # noqa: BLE001 - logged, run continues
                run.failed += 1
                run.log_error(fund["fund_id"], type(exc).__name__, str(exc))

        print(f"[stage 2] llm stub mode: {llm.stub}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
