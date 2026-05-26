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

from core.config_loader import add_workspace_arg
from core.db import funds, partners, source_snapshots, upsert
from core.stage_runner import stage_run
from core.http_client import HttpClient
from core.ids import partner_id_for
from core.llm.client import MODEL_BATCH
from schemas.fund_enrichment import FundEnrichment

STAGE = "02_enrich_funds"
# Required pages: failing to fetch any of these means the fund row
# can't be enriched meaningfully -- the homepage is the canonical
# source for fund identity, thesis, and team lookup. A transport /
# 5xx failure on a REQUIRED path becomes a per-fund run.fail in
# gather_live_pages's caller.
LIVE_PATHS_REQUIRED: tuple[str, ...] = ("",)
# Optional pages: useful context when they fetch, but a missing
# /portfolio or /news is normal and shouldn't bump run.failed.
LIVE_PATHS_OPTIONAL: tuple[str, ...] = (
    "portfolio", "team", "thesis", "about", "news",
)
LIVE_PATHS = list(LIVE_PATHS_REQUIRED + LIVE_PATHS_OPTIONAL)
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

    Non-200 4xx responses are still ignored (a missing /portfolio is
    normal and Attio-like sites often 404 paths that don't exist),
    but transport-layer errors and 5xx responses are surfaced to the
    caller so they can land in run_errors.
    """
    client = HttpClient()
    pages: dict[str, dict] = {}
    required_failures: list[tuple[str, str]] = []
    optional_failures: list[tuple[str, str]] = []
    for path in LIVE_PATHS:
        is_required = path in LIVE_PATHS_REQUIRED
        url = _page_url(fund["domain"], path)
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
        # 4xx on a REQUIRED path is also a hard fund-level problem:
        # we asked for the homepage and got "not here". Optional 4xx
        # stays silent.
        if is_required and res.status >= 400:
            required_failures.append((url, f"HTTP {res.status}"))
    return pages, required_failures, optional_failures


def deterministic_enrichment(pages: dict) -> dict:
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


def _page_html(entry) -> str:
    """Pages may be {url: html_str} (fixture) or {url: {html, final_url}}
    (live; Batch 36 #13). Normalize."""
    if isinstance(entry, dict):
        return entry.get("html", "")
    return entry


def enrich(llm: LLMClient, fund: dict, pages: dict) -> FundEnrichment:
    """Run enrichment. Live: LLM over fetched content. Stub: deterministic."""
    content = "\n\n".join(
        f"--- {url} ---\n{_extract_text(_page_html(html))}"
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


def store_snapshots(engine, pages: dict) -> int:
    """Write each fetched page to source_snapshots, deduped on (url, hash).

    `pages` accepts either:
      - {url: html_str}  (fixture path; final_url stays NULL)
      - {url: {"html": ..., "final_url": ...}}  (live path; final_url is
        captured from the post-redirect httpx response, Batch 36 #13)
    """
    written = 0
    with engine.begin() as conn:
        for url, entry in pages.items():
            if isinstance(entry, dict):
                html = entry.get("html", "")
                final_url = entry.get("final_url")
            else:
                html = entry
                final_url = None
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
            # Slice 18b: register the URL in the canonical sources
            # registry + stamp source_id on the snapshot.
            from core.sources import upsert_source
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 2 fund enrichment.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--fixtures", action="store_true",
        help="Read pages from data/fixtures/fund_pages instead of the network.",
    )
    args = parser.parse_args()
    # Refactor sweep: stage_run() collapses the workspace/preflight/
    # banner/engine/LLM/RunLogger boilerplate. require_anthropic
    # mirrors the live-mode policy.
    with stage_run(
        args, stage=STAGE,
        require_anthropic=not args.fixtures,
    ) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm
        with engine.begin() as conn:
            fund_rows = [
                dict(r._mapping) for r in conn.execute(
                    select(funds).where(funds.c.domain.isnot(None))
                )
            ]
        # Live mode requires an LLM. The deterministic_enrichment() stub is
        # designed for our fixture HTML's <meta name="..."> tags; against a
        # real fund site it would happily return mostly-empty enrichment and
        # the operator would never know. Refuse upfront like Stages 3/4.
        if not args.fixtures and llm.stub:
            ctx.refuse(
                "REFUSED: live Stage 2 requires ANTHROPIC_API_KEY. The "
                "deterministic stub extractor only understands fixture HTML "
                "and would silently produce empty enrichment against real "
                "fund pages. Set the key, or run with --fixtures."
            )
            print(f"[stage 2] REFUSED: see runs.error_summary")
            return ctx.exit_code
        for fund in fund_rows:
            with run.attempt():
                try:
                    if args.fixtures:
                        pages = gather_fixture_pages(fund, ws)
                        required_failures: list[tuple[str, str]] = []
                        optional_failures: list[tuple[str, str]] = []
                    else:
                        pages, required_failures, optional_failures = (
                            asyncio.run(gather_live_pages(fund))
                        )
                    # Launch-blocker fix: a homepage (REQUIRED path)
                    # fetch failure was previously logged but didn't
                    # bump run.failed, so a fund whose homepage 5xx'd
                    # while a couple optional paths fetched could exit
                    # 0 -- a "degraded cleanly" failure mode. Required-
                    # path failures now bump run.failed via run.fail()
                    # so cron / wrappers see exit 2 when enrichment
                    # has degraded broadly. Optional failures still
                    # log for audit but don't bump the counter (a
                    # missing /portfolio is normal site shape).
                    for url, reason in required_failures:
                        run.fail(
                            f"{fund['fund_id']}:{url}",
                            "required_fetch_failed", reason,
                        )
                    for url, reason in optional_failures:
                        run.log_error(
                            f"{fund['fund_id']}:{url}",
                            "optional_fetch_failed", reason,
                        )
                    if not pages:
                        # A fund with zero fetched pages has nothing for
                        # the LLM to enrich from. Previously this counted
                        # as a `skip`, so a workspace where EVERY live
                        # fetch returned 0 pages exited 0 -- a silent
                        # "degraded cleanly" failure mode. Treat per-fund
                        # as a fail so cron / wrappers notice when
                        # enrichment has degraded across the board.
                        run.fail(fund["fund_id"], "no_pages",
                                 "no pages fetched for fund")
                        continue

                    snaps = store_snapshots(engine, pages)
                    enrichment = enrich(llm, fund, pages)

                    # Reconciliation owned by core/fund_enrichment.py
                    # (Refactor item 7 / 11): preserve-on-empty fund row
                    # builder, deterministic partner_id slug, and the
                    # vanished-partner demotion proposal.
                    from core.fund_enrichment import (
                        build_fund_update_values,
                        compute_vanished_partners,
                        partner_upsert_values,
                    )
                    now = _now()
                    update_values = build_fund_update_values(enrichment, now)
                    # Track which partners are still on the team page this
                    # run so we can demote anyone previously seen but now
                    # missing.
                    discovered_pids: set[str] = set()

                    with engine.begin() as conn:
                        conn.execute(
                            funds.update()
                            .where(funds.c.fund_id == fund["fund_id"])
                            .values(**update_values)
                        )
                        for p in enrichment.current_partners:
                            row = partner_upsert_values(
                                fund_id=fund["fund_id"],
                                fund_domain=fund["domain"],
                                partner=p,
                                now=now,
                            )
                            discovered_pids.add(row["partner_id"])
                            upsert(conn, partners, ["partner_id"], row)

                        # Demote previously-seen partners no longer on the
                        # team page. Without this, a partner who left a
                        # fund stays `likely_current` forever and continues
                        # to satisfy Stage 6 criterion 6. An empty
                        # discovered set is treated as an LLM miss (not a
                        # mass departure) and skips the demotion -- see
                        # compute_vanished_partners docstring.
                        prior_for_fund = [
                            r.partner_id for r in conn.execute(
                                select(partners.c.partner_id).where(
                                    partners.c.fund_id == fund["fund_id"]
                                )
                            )
                        ]
                        vanished = compute_vanished_partners(
                            prior_for_fund, discovered_pids,
                        )
                        if vanished:
                            conn.execute(
                                partners.update().where(
                                    partners.c.partner_id.in_(vanished)
                                ).values(
                                    employment_status="uncertain",
                                    last_updated=now,
                                )
                            )
                            print(
                                f"[stage 2] {fund['name']}: demoted "
                                f"{len(vanished)} partner(s) to "
                                f"employment_status=uncertain (no longer on "
                                f"team page)"
                            )
                    print(
                        f"[stage 2] {fund['name']}: {len(pages)} pages "
                        f"({snaps} new snapshots), "
                        f"{len(enrichment.current_partners)} partners, "
                        f"stage={enrichment.stated_stage_focus}"
                    )
                except Exception as exc:  # noqa: BLE001 - logged, run continues
                    run.fail(fund["fund_id"], type(exc).__name__, str(exc))

        print(f"[stage 2] llm stub mode: {llm.stub}")
        # Refactor sweep: ctx.exit_code maps run.failed > 0 to
        # StageResult.OPERATIONAL_FAILURE = 2, preserving the prior
        # behavior from Batch 35.
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
