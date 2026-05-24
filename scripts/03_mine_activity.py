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
from core.validate_config import preflight_or_exit
from core.db import (
    ambiguous_matches, deal_attribution_overrides, deal_attributions,
    funds, get_engine, partners,
)
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
    fid, _candidates = match_fund_with_candidates(name, funds_by_name)
    return fid


# Batch 33 (#341/#342/#737/#738): ambiguous-match-aware fund lookup.
# Returns (chosen_fund_id, candidates_list) where candidates is a list of
# {id, name, score} for the top 3 fuzzy matches sorted by score desc.
# Used by Stage 3 to detect "best match was 0.86 but second-best was
# 0.85 -- this is ambiguous" and log to the ambiguous_matches table.
FUND_NAME_AMBIGUITY_DELTA = 0.05  # within 5% of best => ambiguous
FUND_NAME_AMBIGUITY_FLOOR = 0.70  # ignore candidates below 0.70


def match_fund_with_candidates(
    name: str | None, funds_by_name: dict[str, str],
) -> tuple[str | None, list[dict]]:
    if not name:
        return None, []
    key = normalize_name(name)
    if key in funds_by_name:
        return funds_by_name[key], [{
            "id": funds_by_name[key], "name": key, "score": 1.0,
        }]
    scored: list[tuple[str, str, float]] = []
    for known_name, fid in funds_by_name.items():
        score = token_set_similarity(key, known_name)
        if score >= FUND_NAME_AMBIGUITY_FLOOR:
            scored.append((fid, known_name, score))
    scored.sort(key=lambda x: -x[2])
    candidates = [
        {"id": fid, "name": nm, "score": round(score, 4)}
        for fid, nm, score in scored[:3]
    ]
    if not scored:
        return None, []
    best_id, _best_name, best_score = scored[0]
    if best_score < FUND_NAME_FUZZY_THRESHOLD:
        return None, candidates
    return best_id, candidates


def _detect_ambiguity(candidates: list[dict]) -> bool:
    """True when the top two candidates are within FUND_NAME_AMBIGUITY_DELTA
    of each other (and both above the floor)."""
    if len(candidates) < 2:
        return False
    top = candidates[0]["score"]
    second = candidates[1]["score"]
    return (top - second) < FUND_NAME_AMBIGUITY_DELTA


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


# Batch 32 (#742): provisional fund creation. Synthesize a fund_id from
# the normalized name; flag is_provisional=TRUE so Stage 2 (or the
# operator) can promote it once enriched.
def _create_provisional_fund(engine, name: str) -> str | None:
    from core.ids import fund_id_for
    norm = normalize_name(name).strip()
    if not norm:
        return None
    # Use the normalized name as the synthetic domain so the existing
    # fund_id_for() hash is stable; the row's `domain` column carries
    # `<slug>.provisional` so the operator can distinguish stubs.
    pseudo_domain = norm.replace(" ", "-") + ".provisional"
    fund_id = fund_id_for(pseudo_domain)
    with engine.begin() as conn:
        existing = conn.execute(
            select(funds.c.fund_id).where(funds.c.fund_id == fund_id)
        ).first()
        if not existing:
            conn.execute(funds.insert().values(
                fund_id=fund_id,
                name=name,
                domain=pseudo_domain,
                is_active=True,
                is_provisional=True,
                last_updated=_now(),
            ))
    return fund_id


# Batch 32 (#741): provisional partner creation. Requires a resolvable
# fund (provisional or real); a partner without ANY fund is too
# ambiguous to record without operator review.
def _create_provisional_partner(
    engine, name: str, fund_id: str, fund_id_to_domain: dict[str, str],
) -> str | None:
    norm = (name or "").strip()
    if not norm:
        return None
    domain = fund_id_to_domain.get(fund_id)
    if not domain:
        return None
    pid = partner_id_for(domain, norm)
    with engine.begin() as conn:
        existing = conn.execute(
            select(partners.c.partner_id).where(partners.c.partner_id == pid)
        ).first()
        if not existing:
            conn.execute(partners.insert().values(
                partner_id=pid,
                fund_id=fund_id,
                name=norm,
                employment_status="uncertain",
                is_provisional=True,
                last_updated=_now(),
            ))
    return pid


def _resolve_lead_confidence(
    raw_name: str | None, lead_fund_id: str | None,
    funds_by_name: dict[str, str],
) -> float | None:
    """1.0 for exact normalized match, fuzzy score for fuzzy, None when
    unresolved."""
    if not raw_name or not lead_fund_id:
        return None
    key = normalize_name(raw_name)
    if funds_by_name.get(key) == lead_fund_id:
        return 1.0
    # Fuzzy: find the score that produced the match.
    best = 0.0
    for known_name, fid in funds_by_name.items():
        if fid != lead_fund_id:
            continue
        score = token_set_similarity(key, known_name)
        if score > best:
            best = score
    return best if best > 0 else None


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
    # Batch 32 (#741/#742): when the LLM names a fund/partner the local
    # DB doesn't know about, create a `is_provisional=TRUE` row so the
    # deal can still be attributed. The operator (or a later Stage 2 run)
    # confirms the row by clearing the provisional flag.
    parser.add_argument(
        "--allow-provisional", action="store_true",
        help="Create provisional funds/partners for LLM-named entities "
             "not yet in the DB (Batch 32 #741/#742). Provisional rows "
             "are flagged is_provisional=TRUE so Stage 6 / Stage 8 can "
             "distinguish them from confirmed records.",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    preflight_or_exit(
        ws, stage=STAGE, require_anthropic=not args.fixtures,
    )
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

    # Batch 34: load operator overrides keyed on source_url. The Stage 3
    # per-announcement loop consults this BEFORE asking the LLM so
    # operator decisions survive re-runs.
    overrides_by_url: dict[str, dict] = {}
    with engine.begin() as conn:
        for r in conn.execute(select(deal_attribution_overrides)):
            overrides_by_url[r.source_url] = {
                "action": r.action,
                "lead_fund_id": r.lead_fund_id,
                "attributed_partner_id": r.attributed_partner_id,
                "reason": r.reason,
            }

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
            # Batch 34 (#760): operator override takes precedence over LLM.
            override = overrides_by_url.get(source_url)
            if override and override["action"] == "reject":
                # Wipe any prior attribution rows for this URL (skeleton-
                # only persistence). Don't ask the LLM.
                with engine.begin() as conn:
                    conn.execute(
                        deal_attributions.delete().where(
                            deal_attributions.c.source_url == source_url,
                        )
                    )
                run.skipped += 1
                run.note(
                    f"override REJECT applied to {source_url}: "
                    f"{override['reason']!r}"
                )
                continue
            if override and override["action"] == "set":
                # Skip the LLM entirely; persist a single row built from
                # the operator's chosen fund/partner. (We can't reconstruct
                # the company / round_type without the LLM, so we preserve
                # whatever was already in deal_attributions OR fall through
                # to LLM if no prior row exists.)
                with engine.begin() as conn:
                    existing = conn.execute(
                        select(deal_attributions).where(
                            deal_attributions.c.source_url == source_url,
                        )
                    ).first()
                if existing:
                    with engine.begin() as conn:
                        conn.execute(
                            deal_attributions.update()
                            .where(deal_attributions.c.source_url == source_url)
                            .values(
                                lead_fund_id=override["lead_fund_id"]
                                              or existing.lead_fund_id,
                                attributed_partner_id=(
                                    override["attributed_partner_id"]
                                    or existing.attributed_partner_id
                                ),
                            )
                        )
                    run.succeeded += 1
                    run.note(
                        f"override SET applied to {source_url}: "
                        f"{override['reason']!r}"
                    )
                    continue
                # No prior row -- fall through to the LLM path; we'll
                # apply the override after the LLM result lands.
            try:
                prompt = prompt_template.replace("{ANNOUNCEMENT_TEXT}", ann["text"])
                deal: DealAttribution = llm.complete_json(
                    prompt=prompt,
                    schema=DealAttribution,
                    model=MODEL_BATCH,
                    stub_response=ann.get("_attribution"),
                )
                lead_fund_id, lead_candidates = match_fund_with_candidates(
                    deal.lead_investor, funds_by_name,
                )
                # Batch 33 (#341/#342): when fuzzy match was ambiguous
                # (top two candidates within FUND_NAME_AMBIGUITY_DELTA),
                # record an ambiguous_matches row so the operator can
                # review + resolve. The auto-picked id still wins for
                # this run; resolve_ambiguous_match.py corrects it.
                if (
                    deal.lead_investor
                    and _detect_ambiguity(lead_candidates)
                    and not (
                        lead_candidates
                        and lead_candidates[0]["score"] == 1.0
                    )
                ):
                    with engine.begin() as conn:
                        conn.execute(ambiguous_matches.insert().values(
                            entity_type="fund",
                            raw_name=deal.lead_investor,
                            source_url=source_url,
                            candidates=json.dumps(lead_candidates),
                            chosen_id=lead_fund_id,
                            chosen_score=(
                                lead_candidates[0]["score"]
                                if lead_candidates else None
                            ),
                            captured_at=_now(),
                        ))
                    run.note(
                        f"ambiguous fund match for {deal.lead_investor!r}: "
                        f"chose {lead_fund_id!r} from candidates "
                        f"{lead_candidates}; review via "
                        f"scripts/list_ambiguous_matches.py"
                    )
                # Batch 32 (#742): when --allow-provisional is set AND the
                # named lead investor doesn't resolve, create a
                # provisional fund. We synthesize a fund_id from the
                # normalized name (no real domain yet) so downstream
                # joins work. Stage 2 enrichment can later fill in
                # domain + clear is_provisional.
                if (
                    lead_fund_id is None
                    and args.allow_provisional
                    and deal.lead_investor
                ):
                    lead_fund_id = _create_provisional_fund(
                        engine, deal.lead_investor,
                    )
                    if lead_fund_id:
                        funds_by_name[normalize_name(deal.lead_investor)] = (
                            lead_fund_id
                        )
                        fund_id_to_domain[lead_fund_id] = (
                            f"{lead_fund_id}.provisional"
                        )
                        run.note(
                            f"provisional fund created for "
                            f"{deal.lead_investor!r} (id={lead_fund_id})"
                        )
                sector_tags_json = json.dumps(deal.sector_tags or [])
                # Batch 32: raw names preserved on every row for backfill
                # / audit even when matching dropped the attribution.
                raw_attributed = json.dumps([
                    {"name": ap.name, "fund": ap.fund}
                    for ap in deal.attributed_partners
                ])
                # Match confidence: 1.0 when the lead investor resolved
                # via exact normalized match, fuzzy score when via the
                # fuzzy fallback, None when unresolved. Stage 6 round_fit
                # can filter low-confidence deals.
                match_conf = _resolve_lead_confidence(
                    deal.lead_investor, lead_fund_id, funds_by_name,
                )

                rows: list[dict] = []
                # Batch 27 (#345): when the LLM named partners that we
                # can't resolve to existing partners rows (Stage 2 hasn't
                # discovered them yet), record an audit note so the
                # operator sees "we lost partner attribution for X at
                # fund Y" instead of silently dropping it.
                dropped_partners: list[str] = []
                provisional_partners_created: list[str] = []
                for ap in deal.attributed_partners:
                    ap_fund_id = match_fund(ap.fund, funds_by_name)
                    pid = resolve_partner_id(
                        ap.name, ap_fund_id, fund_id_to_domain, known_partner_ids
                    )
                    # Batch 32 (#741): provisional partner creation. Only
                    # works when we have a resolvable fund (provisional
                    # fund counts) -- a partner without ANY fund is
                    # ambiguous.
                    if (
                        pid is None
                        and args.allow_provisional
                        and ap_fund_id is not None
                        and ap.name
                    ):
                        pid = _create_provisional_partner(
                            engine, ap.name, ap_fund_id,
                            fund_id_to_domain,
                        )
                        if pid:
                            known_partner_ids.add(pid)
                            provisional_partners_created.append(pid)
                            run.note(
                                f"provisional partner created: "
                                f"{ap.name!r}@{ap.fund!r} -> {pid}"
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
                            "raw_lead_investor": deal.lead_investor,
                            "raw_attributed_partners": raw_attributed,
                            "match_confidence": match_conf,
                            "snapshot_id": None,
                        })
                    else:
                        dropped_partners.append(
                            f"{ap.name!r}@{ap.fund!r}"
                        )
                if dropped_partners:
                    run.note(
                        f"unresolved partner(s) for {source_url}: "
                        + ", ".join(dropped_partners)
                        + " (Stage 2 hasn't discovered these partners; "
                        "re-run after enrichment, OR pass --allow-"
                        "provisional to create stub rows; raw names "
                        "preserved in deal_attributions for backfill)"
                    )
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
                        "raw_lead_investor": deal.lead_investor,
                        "raw_attributed_partners": raw_attributed,
                        "match_confidence": match_conf,
                        "snapshot_id": None,
                    })
                # Batch 32: even when there is NO fund match at all, we
                # still record a "skeleton" deal_attributions row with
                # raw names + a NULL lead_fund_id so the unmatched
                # attribution is auditable and backfillable. The row
                # carries no partner attribution (Stage 6 ignores
                # lead_fund_id=NULL rows) but the audit trail captures
                # the LLM's intent.
                if not rows and not lead_fund_id and deal.lead_investor:
                    rows.append({
                        "company": deal.company,
                        "round_type": deal.round_type,
                        "round_size_usd": deal.round_size_usd,
                        "announcement_date": deal.announcement_date,
                        "lead_fund_id": None,
                        "attributed_partner_id": None,
                        "source_url": source_url,
                        "sector_tags": sector_tags_json,
                        "captured_at": _now(),
                        "raw_lead_investor": deal.lead_investor,
                        "raw_attributed_partners": raw_attributed,
                        "match_confidence": None,
                        "snapshot_id": None,
                    })
                    run.note(
                        f"unmatched lead investor for {source_url}: "
                        f"{deal.lead_investor!r} (recorded as skeleton "
                        f"row for backfill; pass --allow-provisional to "
                        f"create the fund stub)"
                    )
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
