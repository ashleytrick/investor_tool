"""Stage 8: optional sync to Attio.

For each active fund -> upsert a Companies record (matching attribute: domains).
For each scored partner -> match by email -> by linkedin_url query -> by name +
company-link query, then PATCH (if existing) or POST (if new). The recommended
default order is from PROJECT_BRIEF.

Preserve-on-outreach-started: if the existing partner record has
outreach_status in attio.yaml.preserve_on_outreach_started.statuses, the
preserved_fields listed there are omitted from the update payload.

Manual override protection: if existing record has manual_score_override=TRUE,
all score fields listed in attio.yaml.manual_override_protection are omitted.
Same for manual_recommended_override -> recommended_to_send.

Every API operation logs a row to attio_sync_log.

If the workspace has no attio.yaml or no ATTIO_API_KEY, exits 0 with a clear
skip message (the CSV path runs without Attio).

Run: uv run scripts/08_sync_to_attio.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.attio_client import AttioClient, AttioError, AttioNotConfigured
from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.db import (
    attio_sync_log,
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    get_engine,
    partner_score_summaries,
    partners,
)
from core.runs import RunLogger

STAGE = "08_sync_to_attio"

# Attribute keys that Attio represents as single-select values (must be sent
# as [{"option": {"title": "..."}}]). Everything else uses [{"value": ...}].
SELECT_SLUGS: set[str] = {
    "stage_focus", "score_confidence", "email_strategy_used",
    "email_alternate_strategy", "template_smell", "outreach_status",
    "meeting_outcome", "reply_type",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _wrap_value(value, api_slug: str):
    """Convert a raw db value to the Attio v2 value-list shape."""
    if value is None or value == "":
        return None
    if api_slug in SELECT_SLUGS:
        return [{"option": {"title": str(value)}}]
    if isinstance(value, bool):
        return [{"value": value}]
    if isinstance(value, (int, float)):
        return [{"value": value}]
    if hasattr(value, "isoformat"):
        return [{"value": value.isoformat()}]
    return [{"value": str(value)}]


def build_payload(attr_map: dict[str, str], source: dict) -> dict:
    """For each (db_key, api_slug) pair, pull source[db_key] and wrap."""
    payload: dict = {}
    for db_key, api_slug in attr_map.items():
        wrapped = _wrap_value(source.get(db_key), api_slug)
        if wrapped is not None:
            payload[api_slug] = wrapped
    return payload


def log_sync(engine, *, object_type, local_id, attio_record_id, operation,
             success, error_message=None):
    with engine.begin() as conn:
        conn.execute(attio_sync_log.insert().values(
            object_type=object_type,
            local_id=local_id,
            attio_record_id=attio_record_id,
            operation=operation,
            success=success,
            error_message=error_message,
            synced_at=_now(),
        ))


def existing_partner_state(record: dict) -> dict:
    """Pull the fields we care about for preserve/override checks."""
    values = (record or {}).get("values") or {}

    def _scalar(slug: str):
        v = values.get(slug)
        if not v:
            return None
        return v[0].get("value") if isinstance(v, list) else v

    def _option(slug: str):
        v = values.get(slug)
        if not v:
            return None
        try:
            return v[0]["option"]["title"]
        except Exception:  # noqa: BLE001
            return None

    return {
        "outreach_status": _option("outreach_status"),
        "manual_score_override": bool(_scalar("manual_score_override")),
        "manual_recommended_override": bool(_scalar("manual_recommended_override")),
    }


def strip_preserved_fields(
    payload: dict,
    existing: dict,
    attio_cfg: dict,
    attr_map: dict[str, str],
) -> tuple[dict, list[str]]:
    """Remove fields per preserve-on-outreach-started + manual-override rules."""
    removed: list[str] = []
    preserve = attio_cfg.get("preserve_on_outreach_started") or {}
    preserve_statuses = set(preserve.get("statuses") or [])
    preserve_fields = set(preserve.get("preserved_fields") or [])
    if existing.get("outreach_status") in preserve_statuses:
        for db_key in preserve_fields:
            api_slug = attr_map.get(db_key, db_key)
            if api_slug in payload:
                payload.pop(api_slug)
                removed.append(api_slug)

    mop = attio_cfg.get("manual_override_protection") or {}
    if existing.get("manual_score_override"):
        for db_key in mop.get("if_manual_score_override_true_preserve") or []:
            api_slug = attr_map.get(db_key, db_key)
            if api_slug in payload:
                payload.pop(api_slug)
                removed.append(api_slug)
    if existing.get("manual_recommended_override"):
        for db_key in mop.get("if_manual_recommended_override_true_preserve") or []:
            api_slug = attr_map.get(db_key, db_key)
            if api_slug in payload:
                payload.pop(api_slug)
                removed.append(api_slug)
    return payload, removed


def find_partner_record(client: AttioClient, person_object: str, *, email: str | None,
                        linkedin_url: str | None, name: str | None,
                        company_record_id: str | None) -> dict | None:
    """Return existing person record by email -> linkedin_url -> name+company."""
    if email:
        results = client.query_records(person_object, {
            "email_addresses": email
        }, limit=1)
        if results:
            return results[0]
    if linkedin_url:
        results = client.query_records(person_object, {
            "linkedin_url": linkedin_url
        }, limit=1)
        if results:
            return results[0]
    if name and company_record_id:
        results = client.query_records(person_object, {
            "name": name,
            "company": {"target_record_id": company_record_id},
        }, limit=2)
        if len(results) == 1:
            return results[0]
        if len(results) > 1:
            return {"_conflict": True, "candidates": results}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 8 Attio sync.")
    add_workspace_arg(parser)
    parser.add_argument("--top", type=int, default=25,
                        help="Top-N partners by send_now_priority to sync.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    cfg = ws.attio or {}
    attio_cfg = cfg.get("attio") or cfg

    with RunLogger(engine, ws.name, STAGE) as run:
        if not attio_cfg:
            print(f"[stage 8] no attio.yaml in workspace {ws.name!r}; skipping")
            run.skipped = 1
            return 0
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            print(f"[stage 8] {exc}; skipping")
            run.skipped = 1
            return 0

        objects = attio_cfg.get("objects") or {"funds": "companies", "partners": "people"}
        fund_object = objects["funds"]
        person_object = objects["partners"]
        fund_attr_map = attio_cfg.get("fund_attributes") or {}
        partner_attr_map = attio_cfg.get("partner_attributes") or {}
        matching = attio_cfg.get("matching_attributes") or {
            "companies": "domains", "people": "email_addresses",
        }

        # ---- 1) Upsert funds as companies ----
        with engine.begin() as conn:
            fund_rows = list(conn.execute(select(funds)))
        fund_attio_ids: dict[str, str] = {}
        for f in fund_rows:
            run.processed += 1
            source = dict(f._mapping)
            # `domains` on Attio takes a list of {domain: "..."} objects.
            base_payload = {
                "name": [{"value": f.name}],
                "domains": [{"domain": f.domain}] if f.domain else None,
            }
            base_payload = {k: v for k, v in base_payload.items() if v is not None}
            custom_payload = build_payload(fund_attr_map, source)
            payload = {**base_payload, **custom_payload}
            try:
                result = client.upsert_record(
                    fund_object, matching[fund_object], payload
                )
                attio_id = (result.get("data") or {}).get("id", {}).get("record_id")
                if attio_id:
                    fund_attio_ids[f.fund_id] = attio_id
                    with engine.begin() as conn:
                        conn.execute(
                            funds.update().where(funds.c.fund_id == f.fund_id).values(
                                attio_record_id=attio_id, last_updated=_now(),
                            )
                        )
                log_sync(engine, object_type="company", local_id=f.fund_id,
                         attio_record_id=attio_id, operation="upsert", success=True)
                run.succeeded += 1
            except AttioError as exc:
                log_sync(engine, object_type="company", local_id=f.fund_id,
                         attio_record_id=None, operation="upsert", success=False,
                         error_message=str(exc))
                run.failed += 1
                run.log_error(f.fund_id, "AttioError", str(exc))

        # ---- 2) Sync partners as people, top-N by send_now_priority ----
        with engine.begin() as conn:
            partner_rows = list(conn.execute(
                select(
                    partner_score_summaries,
                    partners.c.name.label("partner_name"),
                    partners.c.title,
                    partners.c.linkedin_url,
                    partners.c.warm_path_available,
                    partners.c.warm_path_contact,
                    partners.c.fund_id,
                    partners.c.attio_record_id.label("known_attio_id"),
                )
                .join(partners, partners.c.partner_id == partner_score_summaries.c.partner_id)
                .order_by(partner_score_summaries.c.send_now_priority.desc())
                .limit(args.top)
            ))
            # Pull recommended email draft + followup + deck per partner.
            drafts_by_partner: dict[str, dict] = {}
            for d in conn.execute(select(email_drafts)):
                if d.is_recommended:
                    drafts_by_partner.setdefault(d.partner_id, {})["recommended"] = d
                else:
                    drafts_by_partner.setdefault(d.partner_id, {})["alternate"] = d
            followups_by_partner = {
                f.partner_id: f.body for f in conn.execute(select(followup_drafts))
            }
            deck_by_partner = {
                d.partner_id: d.body
                for d in conn.execute(select(deck_request_responses))
            }

        for p in partner_rows:
            run.processed += 1
            try:
                drafts = drafts_by_partner.get(p.partner_id, {})
                rec = drafts.get("recommended")
                alt = drafts.get("alternate")
                source = dict(p._mapping)
                source.update({
                    "outreach_email_draft": rec.body if rec else None,
                    "email_strategy_used": rec.strategy if rec else None,
                    "email_subject_line": rec.subject if rec else None,
                    "conversion_hypothesis": rec.conversion_hypothesis if rec else None,
                    "likely_objection": rec.likely_objection if rec else None,
                    "objection_preempted": rec.objection_preempted if rec else None,
                    "email_draft_alternate": alt.body if alt else None,
                    "email_alternate_strategy": alt.strategy if alt else None,
                    "template_smell": rec.template_smell if rec else None,
                    "followup_email_draft": followups_by_partner.get(p.partner_id),
                    "deck_request_response": deck_by_partner.get(p.partner_id),
                })
                custom_payload = build_payload(partner_attr_map, source)

                fund_attio_id = (
                    fund_attio_ids.get(p.fund_id)
                    or _lookup_fund_attio_id(engine, p.fund_id)
                )
                base_payload = {
                    "name": [{"value": p.partner_name}],
                }
                if fund_attio_id:
                    base_payload["company"] = [{
                        "target_object": fund_object,
                        "target_record_id": fund_attio_id,
                    }]
                payload = {**base_payload, **custom_payload}

                # Find existing record per match strategy.
                match = find_partner_record(
                    client, person_object,
                    email=None,
                    linkedin_url=p.linkedin_url,
                    name=p.partner_name,
                    company_record_id=fund_attio_id,
                )
                attio_id = None
                op = None
                if match and match.get("_conflict"):
                    log_sync(engine, object_type="person", local_id=p.partner_id,
                             attio_record_id=None, operation="skip_conflict",
                             success=False,
                             error_message=f"{len(match['candidates'])} candidate matches")
                    run.skipped += 1
                    continue
                if match:
                    existing_state = existing_partner_state(match)
                    payload, removed = strip_preserved_fields(
                        payload, existing_state, attio_cfg, partner_attr_map
                    )
                    attio_id = match.get("id", {}).get("record_id")
                    op = "patch"
                    if attio_id and payload:
                        client.update_record(person_object, attio_id, payload)
                else:
                    result = client.create_record(person_object, payload)
                    attio_id = (result.get("data") or {}).get("id", {}).get("record_id")
                    op = "create"

                if attio_id:
                    with engine.begin() as conn:
                        conn.execute(
                            partners.update().where(
                                partners.c.partner_id == p.partner_id
                            ).values(
                                attio_record_id=attio_id, last_updated=_now(),
                            )
                        )
                log_sync(engine, object_type="person", local_id=p.partner_id,
                         attio_record_id=attio_id, operation=op, success=True)
                run.succeeded += 1
            except AttioError as exc:
                log_sync(engine, object_type="person", local_id=p.partner_id,
                         attio_record_id=None, operation="error",
                         success=False, error_message=str(exc))
                run.failed += 1
                run.log_error(p.partner_id, "AttioError", str(exc))

        client.close()
        print(f"[stage 8] synced {run.succeeded} record(s); "
              f"failed={run.failed} skipped={run.skipped}")

    return 0


def _lookup_fund_attio_id(engine, fund_id: str) -> str | None:
    with engine.begin() as conn:
        row = conn.execute(
            select(funds.c.attio_record_id).where(funds.c.fund_id == fund_id)
        ).first()
    return row.attio_record_id if row else None


if __name__ == "__main__":
    raise SystemExit(main())
