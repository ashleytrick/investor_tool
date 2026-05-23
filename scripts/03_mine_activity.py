"""Stage 3: mine recent funding announcements and attribute deals.

For each announcement, runs the deal-attribution LLM (prompts/attribute_deal.txt),
validates against schemas/deal_attribution.py, resolves the lead investor to a
known fund and any named champions to known partners, writes deal_attributions
rows, and recomputes funds.last_known_activity_date / is_active.

Fixture mode (--fixtures): reads data/fixtures/announcements.json. Each fixture
entry carries a `_attribution` dict that is passed to the LLM client as the
stub_response, so the run is fully offline.

Run: uv run scripts/03_mine_activity.py --workspace clients/test_workspace --fixtures
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import deal_attributions, funds, get_engine, partners
from core.http_client import HttpClient
from core.ids import normalize_name, partner_id_for
from core.llm.client import MODEL_BATCH, LLMClient
from core.runs import RunLogger
from core.similarity import token_set_similarity
from schemas.deal_attribution import DealAttribution

STAGE = "03_mine_activity"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "attribute_deal.txt"
ACTIVE_WINDOW_DAYS = 365  # is_active = activity in last 12 months
FUND_NAME_FUZZY_THRESHOLD = 0.85
RSS_LOOKBACK_DAYS = 365  # only attribute announcements published in last N days


def _fetch_live_rss_announcements(feeds: list[dict]) -> list[dict]:
    """Pull RSS items from each feed in sources.yaml.funding_announcement_feeds.

    Returns a list of {"source_url", "text"} dicts ready for LLM attribution.
    Items older than RSS_LOOKBACK_DAYS are filtered out (per brief: 12 months).
    """
    if not feeds:
        return []
    try:
        import feedparser
    except ImportError:
        print(
            "[stage 3] feedparser not installed; run `uv sync`. "
            "Skipping live RSS."
        )
        return []
    client = HttpClient()
    cutoff = date.today().toordinal() - RSS_LOOKBACK_DAYS
    out: list[dict] = []
    for feed_cfg in feeds:
        url = feed_cfg.get("url")
        name = feed_cfg.get("name") or url
        if not url:
            continue
        try:
            res = asyncio.run(client.fetch(url))
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't kill the run
            print(f"[stage 3] feed {name!r} fetch failed: {exc}")
            continue
        if res.status != 200 or not res.text:
            print(f"[stage 3] feed {name!r} returned HTTP {res.status}")
            continue
        feed = feedparser.parse(res.text)
        for entry in getattr(feed, "entries", []):
            link = getattr(entry, "link", None) or getattr(entry, "id", None)
            if not link:
                continue
            published = getattr(entry, "published_parsed", None)
            if published is not None:
                try:
                    pub_ord = date(
                        published.tm_year, published.tm_mon, published.tm_mday
                    ).toordinal()
                    if pub_ord < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            content_blocks = getattr(entry, "content", []) or []
            body = " ".join(
                [title, summary]
                + [getattr(b, "value", "") for b in content_blocks]
            ).strip()
            if body:
                out.append({"source_url": link, "text": body})
        print(f"[stage 3] feed {name!r}: {len(feed.entries)} entries pulled")
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def match_fund(name: str | None, funds_by_name: dict[str, str]) -> str | None:
    """Resolve a fund name to a known fund_id. Exact normalized match, then fuzzy."""
    if not name:
        return None
    key = normalize_name(name)
    if key in funds_by_name:
        return funds_by_name[key]
    # Fuzzy fallback against known fund names.
    best_id, best_score = None, 0.0
    for known_name, fid in funds_by_name.items():
        score = token_set_similarity(key, known_name)
        if score > best_score:
            best_id, best_score = fid, score
    return best_id if best_score >= FUND_NAME_FUZZY_THRESHOLD else None


def resolve_partner_id(
    partner_name: str,
    fund_id: str | None,
    fund_id_to_domain: dict[str, str],
    known_partner_ids: set[str],
) -> str | None:
    """Return partner_id only if computed slug exists in the partners table."""
    if not fund_id:
        return None
    domain = fund_id_to_domain.get(fund_id)
    if not domain:
        return None
    pid = partner_id_for(domain, partner_name)
    return pid if pid in known_partner_ids else None


def recompute_fund_activity(engine) -> None:
    """Idempotent: set last_known_activity_date + is_active from deal_attributions."""
    from core.dates import within_days
    today = date.today()
    with engine.begin() as conn:
        for fid, in conn.execute(select(funds.c.fund_id)):
            max_date = conn.execute(
                select(deal_attributions.c.announcement_date)
                .where(deal_attributions.c.lead_fund_id == fid)
                .order_by(deal_attributions.c.announcement_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            # within_days rejects future dates; bad announcement_date parsing
            # can no longer mark a fund "active" via a 2027 ghost date.
            is_active = within_days(max_date, ACTIVE_WINDOW_DAYS, today)
            conn.execute(
                funds.update().where(funds.c.fund_id == fid).values(
                    last_known_activity_date=max_date,
                    is_active=is_active,
                    last_updated=_now(),
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3 recent-activity mining.")
    add_workspace_arg(parser)
    parser.add_argument("--fixtures", action="store_true",
                        help="Read announcements from data/fixtures/announcements.json")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)

    # Lookup maps for fund + partner resolution.
    with engine.begin() as conn:
        fund_rows = list(conn.execute(select(funds.c.fund_id, funds.c.name, funds.c.domain)))
        partner_rows = list(conn.execute(select(partners.c.partner_id)))
    funds_by_name = {normalize_name(r.name): r.fund_id for r in fund_rows}
    fund_id_to_domain = {r.fund_id: r.domain for r in fund_rows}
    known_partner_ids = {r.partner_id for r in partner_rows}

    feeds = ws.sources.get("funding_announcement_feeds") or []
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        # Source announcements (enter RunLogger BEFORE the ingest check so the
        # run row records the failure visibly in `runs` / status.py).
        if args.fixtures:
            announcements = json.loads(
                (ws.fixtures_dir / "announcements.json").read_text(encoding="utf-8")
            )
        else:
            announcements = _fetch_live_rss_announcements(feeds)
            if not announcements:
                if feeds:
                    msg = (
                        f"FAIL: {len(feeds)} feed(s) configured but 0 usable "
                        f"announcements ingested. Check feed reachability + "
                        f"recent-item dates."
                    )
                    print(f"[stage 3] {msg}")
                    run.note(msg)
                    run.failed = len(feeds)
                    return 2
                print(
                    "[stage 3] no announcements ingested; sources.yaml has no "
                    "funding_announcement_feeds and --fixtures wasn't passed"
                )
            if llm.stub and announcements:
                msg = (
                    f"REFUSED: {len(announcements)} live announcements "
                    f"fetched but llm is in stub mode (no ANTHROPIC_API_KEY). "
                    f"Set the key, or run with --fixtures."
                )
                print(f"[stage 3] {msg}")
                run.note(msg)
                run.failed = 1
                return 2
        partner_attributed = 0
        for ann in announcements:
            run.processed += 1
            source_url = ann["source_url"]
            try:
                prompt = prompt_template.replace("{ANNOUNCEMENT_TEXT}", ann["text"])
                deal: DealAttribution = llm.complete_json(
                    prompt=prompt,
                    schema=DealAttribution,
                    model=MODEL_BATCH,
                    stub_response=ann.get("_attribution"),
                )
                lead_fund_id = match_fund(deal.lead_investor, funds_by_name)
                sector_tags_json = json.dumps(deal.sector_tags or [])
                rows: list[dict] = []
                for ap in deal.attributed_partners:
                    ap_fund_id = match_fund(ap.fund, funds_by_name)
                    pid = resolve_partner_id(
                        ap.name, ap_fund_id, fund_id_to_domain, known_partner_ids
                    )
                    if pid:
                        rows.append({
                            "company": deal.company,
                            "round_type": deal.round_type,
                            "round_size_usd": deal.round_size_usd,
                            "announcement_date": deal.announcement_date,
                            "lead_fund_id": lead_fund_id,
                            "attributed_partner_id": pid,
                            "source_url": source_url,
                            "sector_tags": sector_tags_json,
                            "captured_at": _now(),
                        })
                if not rows and lead_fund_id:
                    rows.append({
                        "company": deal.company,
                        "round_type": deal.round_type,
                        "round_size_usd": deal.round_size_usd,
                        "announcement_date": deal.announcement_date,
                        "lead_fund_id": lead_fund_id,
                        "attributed_partner_id": None,
                        "source_url": source_url,
                        "sector_tags": sector_tags_json,
                        "captured_at": _now(),
                    })
                # Delete prior attributions for THIS source_url FIRST. If a
                # reprocess yields no known-fund/partner rows, the prior
                # (possibly wrong) attribution must NOT linger -- otherwise
                # corrections silently fail to remove stale data.
                with engine.begin() as conn:
                    conn.execute(
                        delete(deal_attributions).where(
                            deal_attributions.c.source_url == source_url
                        )
                    )
                    if rows:
                        conn.execute(deal_attributions.insert(), rows)
                if not rows:
                    run.skipped += 1
                    continue
                partner_attributed += sum(1 for r in rows if r["attributed_partner_id"])
                run.succeeded += 1
            except Exception as exc:  # noqa: BLE001 - logged, continue
                run.failed += 1
                run.log_error(source_url, type(exc).__name__, str(exc))

        recompute_fund_activity(engine)
        print(
            f"[stage 3] {run.succeeded} deals attributed to known funds, "
            f"{partner_attributed} with a specific partner"
        )
        print(f"[stage 3] llm stub mode: {llm.stub}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
