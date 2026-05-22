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
import json
import pathlib
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg, load_workspace
from core.db import deal_attributions, funds, get_engine, partners
from core.ids import normalize_name, partner_id_for
from core.llm.client import MODEL_BATCH, LLMClient
from core.runs import RunLogger
from core.similarity import token_set_similarity
from schemas.deal_attribution import DealAttribution

STAGE = "03_mine_activity"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "attribute_deal.txt"
ACTIVE_WINDOW_DAYS = 365  # is_active = activity in last 12 months
FUND_NAME_FUZZY_THRESHOLD = 0.85


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
    today = date.today()
    with engine.begin() as conn:
        for fid, in conn.execute(select(funds.c.fund_id)):
            max_date = conn.execute(
                select(deal_attributions.c.announcement_date)
                .where(deal_attributions.c.lead_fund_id == fid)
                .order_by(deal_attributions.c.announcement_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            is_active = (
                max_date is not None
                and (today - max_date).days <= ACTIVE_WINDOW_DAYS
            )
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
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)

    # Lookup maps for fund + partner resolution.
    with engine.begin() as conn:
        fund_rows = list(conn.execute(select(funds.c.fund_id, funds.c.name, funds.c.domain)))
        partner_rows = list(conn.execute(select(partners.c.partner_id)))
    funds_by_name = {normalize_name(r.name): r.fund_id for r in fund_rows}
    fund_id_to_domain = {r.fund_id: r.domain for r in fund_rows}
    known_partner_ids = {r.partner_id for r in partner_rows}

    # Source announcements.
    if args.fixtures:
        announcements = json.loads(
            (ws.fixtures_dir / "announcements.json").read_text(encoding="utf-8")
        )
    else:
        announcements = []  # Live RSS support arrives with Stage 3 production use.
        print("[stage 3] no --fixtures flag and live RSS not configured; nothing to do")

    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
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
                        "captured_at": _now(),
                    })
                if not rows:
                    run.skipped += 1
                    continue
                with engine.begin() as conn:
                    conn.execute(
                        delete(deal_attributions).where(
                            deal_attributions.c.source_url == source_url
                        )
                    )
                    conn.execute(deal_attributions.insert(), rows)
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
