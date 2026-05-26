"""Stage 2 LLM extraction + fixture-mode deterministic stub (Slice 18c).

Two functions:

  - deterministic_enrichment(pages) -> dict
        Offline extractor for fixture HTML. Reads `<meta>` tags and
        `<div class="partner">` / `<li class="portfolio-company">` nodes
        out of the canned fixture format. Used as the stub_response for
        the LLM client when no ANTHROPIC_API_KEY is set.

  - enrich(llm, fund, pages) -> FundEnrichment
        Concatenate the fetched pages into one text blob, format the
        prompt template, hand to llm.complete_json with the
        deterministic stub as fallback. Live path = real LLM; stub path
        = deterministic extractor over fixture HTML.

Lifted verbatim from scripts/02_enrich_funds.py; signatures unchanged
so any external caller importing these from the script keeps working
through the back-compat re-exports there.
"""
from __future__ import annotations

import pathlib

from selectolax.parser import HTMLParser

from core.llm.client import LLMClient, MODEL_BATCH
from core.stage2.fetch import _page_html, extract_text
from schemas.fund_enrichment import FundEnrichment


PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "prompts" / "enrich_fund.txt"
)


def deterministic_enrichment(pages: dict) -> dict:
    """Offline stub: extract enrichment from the structured fixture HTML.

    Designed for the fixture HTML format (meta tags + .partner /
    .portfolio-company nodes). The live LLM path handles arbitrary
    real fund sites.
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
    for entry in pages.values():
        html = _page_html(entry)
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
            out["stated_sectors"] = [
                x.strip() for x in sec.split(",") if x.strip()
            ]
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


def enrich(llm: LLMClient, fund: dict, pages: dict) -> FundEnrichment:
    """Run enrichment. Live: LLM over fetched content. Stub: deterministic."""
    content = "\n\n".join(
        f"--- {url} ---\n{extract_text(_page_html(html))}"
        for url, html in pages.items()
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
