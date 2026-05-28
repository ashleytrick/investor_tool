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
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.approval.persistence import stale_live_approvals_for_partner
from core.approval.state_machine import TRIGGER_EMPLOYMENT_LEFT_FUND
from core.config_loader import add_workspace_arg
from core.db import funds, partners, upsert
from core.ids import partner_id_for
from core.stage_runner import stage_run

# Slice 18c: fetch + extract logic moved to core/stage2/. Re-exported
# below as module-level names so any external caller that imports
# from this script keeps working.
from core.stage2.fetch import (  # noqa: F401
    LIVE_PATHS,
    LIVE_PATHS_OPTIONAL,
    LIVE_PATHS_REQUIRED,
    STAGE,
    extract_text as _extract_text,
    gather_fixture_pages,
    gather_live_pages,
    page_url as _page_url,
    store_snapshots,
)
from core.stage2.extract import (  # noqa: F401
    PROMPT_PATH,
    deterministic_enrichment,
    enrich,
)
from core.stage2.partner_links import harvest_partner_links


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
                    # Track which partners are still on the team page this
                    # run so we can demote anyone previously seen but now
                    # missing.
                    discovered_pids: set[str] = set()
                    vanished: set[str] = set()

                    with engine.begin() as conn:
                        # Slice 18b follow-up (#18): pass `conn` so the
                        # builder can upsert into the sources registry +
                        # produce a source_ids JSON list alongside the
                        # legacy semicolon-delimited source_urls.
                        update_values = build_fund_update_values(
                            enrichment, now, conn=conn,
                        )
                        conn.execute(
                            funds.update()
                            .where(funds.c.fund_id == fund["fund_id"])
                            .values(**update_values)
                        )
                        partner_name_to_id: dict[str, str] = {}
                        for p in enrichment.current_partners:
                            row = partner_upsert_values(
                                fund_id=fund["fund_id"],
                                fund_domain=fund["domain"],
                                partner=p,
                                now=now,
                            )
                            discovered_pids.add(row["partner_id"])
                            partner_name_to_id[p.name] = row["partner_id"]
                            upsert(conn, partners, ["partner_id"], row)

                        # Batch J: deterministically harvest LinkedIn +
                        # Twitter URLs for these partners from the same
                        # team-page HTML. Removes the operator's CSV
                        # work for the common case where the team page
                        # already links to each partner's profile.
                        link_map = harvest_partner_links(
                            pages, list(partner_name_to_id),
                        )
                        for pname, links in link_map.items():
                            pid = partner_name_to_id.get(pname)
                            if not pid:
                                continue
                            update_vals: dict = {}
                            if links.get("linkedin_url"):
                                update_vals["linkedin_url"] = (
                                    links["linkedin_url"]
                                )
                            if links.get("twitter_handle"):
                                update_vals["twitter_handle"] = (
                                    links["twitter_handle"]
                                )
                            if not update_vals:
                                continue
                            conn.execute(
                                partners.update()
                                .where(partners.c.partner_id == pid)
                                .values(**update_vals, last_updated=now)
                            )

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
                    for pid in vanished:
                        staled = stale_live_approvals_for_partner(
                            engine,
                            partner_id=pid,
                            trigger=TRIGGER_EMPLOYMENT_LEFT_FUND,
                            notes=(
                                "Stage 2 enrichment no longer found this "
                                "partner on the fund team page; employment "
                                "status demoted to uncertain"
                            ),
                        )
                        if staled:
                            run.note(
                                f"staled {staled} approved draft(s) for "
                                f"{pid} after Stage 2 demotion"
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
