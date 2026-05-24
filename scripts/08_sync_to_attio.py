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

from sqlalchemy import func, select

from core.attio_client import AttioClient, AttioError, AttioNotConfigured
from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.production_guards import production_gate_for_attio_sync
from core.validate_config import preflight_or_exit
from core.db import (
    attio_sync_log,
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    get_engine,
    partner_score_summaries,
    partners,
    runs,
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
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build payloads + run match cascade against Attio, but DO NOT "
             "write. Prints what would change. Finding 41.",
    )
    parser.add_argument(
        "--require-ready-to-send", action="store_true",
        help="Sync only partners whose outreach_status is ready_to_send "
             "AND whose recommended draft has qa_status='pass'. Use this "
             "before a real send batch (Findings 42, 43).",
    )
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit RFC 2606 reserved domains (.example/.test/.invalid) "
             "in fund domains and partner emails. Use for fixture / smoke "
             "runs ONLY; production sync should refuse fictional data so "
             "fixture leakage cannot pollute a real Attio workspace.",
    )
    # Batch 30 (#529/#531): refuse to sync a workspace whose company.yaml
    # declares mode=fixture without an explicit override.
    parser.add_argument(
        "--allow-fixture-mode", action="store_true",
        help="Bypass the mode=fixture refusal. Required when the workspace's "
             "company.yaml has `mode: fixture` -- prevents accidental syncs "
             "of fictional data to real Attio.",
    )
    # Batch 38 (#46): production workspaces should fail HARD when Attio
    # isn't configured, not skip. Distinct from --allow-skip on Stage 0:
    # Stage 0 is a verification, Stage 8 is the actual write.
    parser.add_argument(
        "--require-attio", action="store_true",
        help="Refuse to skip when attio.yaml or ATTIO_API_KEY is missing. "
             "Use in production cron entries that depend on a real Attio "
             "sync (Batch 38 #46).",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    preflight_or_exit(
        ws, stage=STAGE, require_attio=bool(ws.attio),
    )
    print_banner(ws, stage=STAGE)
    # Batch 30 (#529/#531): mode-aware refusal. A workspace explicitly
    # marked `mode: fixture` should never sync to real Attio without
    # the operator stating the intent.
    if ws.mode == "fixture" and not args.allow_fixture_mode:
        msg = (
            f"REFUSED: workspace mode=fixture; sync to Attio would "
            f"propagate fictional data. Pass --allow-fixture-mode if "
            f"you really intend to test against Attio."
        )
        print(f"[stage 8] {msg}")
        return 2
    engine = get_engine(ws.db_url)
    cfg = ws.attio or {}
    attio_cfg = cfg.get("attio") or cfg

    with RunLogger(engine, ws.name, STAGE) as run:
        # Batch 38 (#46): --require-attio turns the skip-on-missing-config
        # path into a hard failure. ws.mode == "prod" also implies
        # require-attio so a prod workspace can't quietly skip its CRM
        # sync without the operator noticing.
        require = args.require_attio or ws.mode == "prod"
        if not attio_cfg:
            msg = f"no attio.yaml in workspace {ws.name!r}"
            if require:
                print(f"[stage 8] REFUSED: {msg} (require-attio)")
                run.note(f"REFUSED: {msg}")
                run.failed = 1
                return 2
            print(f"[stage 8] {msg}; skipping")
            run.skipped = 1
            return 0
        try:
            client = AttioClient.from_workspace(ws)
        except AttioNotConfigured as exc:
            if require:
                print(f"[stage 8] REFUSED: {exc} (require-attio)")
                run.note(f"REFUSED: {exc}")
                run.failed = 1
                return 2
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
        # Finding 6: brief Stage 8 says "for each active fund". Plus any
        # fund that has a top-N recommended partner being synced this run
        # -- otherwise the person->company link from step 2 dangles.
        with engine.begin() as conn:
            top_partner_fund_ids = [
                r.fund_id for r in conn.execute(
                    select(partners.c.fund_id)
                    .join(
                        partner_score_summaries,
                        partner_score_summaries.c.partner_id == partners.c.partner_id,
                    )
                    .where(partner_score_summaries.c.recommended_to_send.is_(True))
                    .order_by(partner_score_summaries.c.send_now_priority.desc())
                    .limit(args.top)
                )
            ]
            fund_rows = list(conn.execute(
                select(funds).where(
                    (funds.c.is_active.is_(True))
                    | (funds.c.fund_id.in_(top_partner_fund_ids or [""]))
                )
            ))
        fund_attio_ids: dict[str, str] = {}
        for f in fund_rows:
            run.processed += 1
            # Batch 9 production guard: refuse to push fictional fixture
            # data (.example/.test/.invalid domains) to a real Attio
            # workspace. --allow-example-domains lets fixture / smoke
            # runs through.
            prod_fails = production_gate_for_attio_sync(
                fund_domain=f.domain, partner_email=None,
            )
            if prod_fails and not args.allow_example_domains:
                run.skipped += 1
                msg = (
                    f"refused fund {f.fund_id}: "
                    + "; ".join(prod_fails)
                    + " (pass --allow-example-domains to override)"
                )
                print(f"[stage 8] PROD GUARD: {msg}")
                run.note(msg)
                log_sync(
                    engine, object_type="company", local_id=f.fund_id,
                    attio_record_id=None, operation="skip_prod_guard",
                    success=False, error_message="; ".join(prod_fails),
                )
                continue
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
                # Batch 38 (#52): record the payload to attio_sync_log
                # BEFORE the API call so the operator can replay / debug.
                # In dry-run mode this is the only record of what would
                # have been sent.
                log_sync(
                    engine, object_type="company", local_id=f.fund_id,
                    attio_record_id=None,
                    operation="dry_run_preview" if args.dry_run else "upsert_attempt",
                    success=True,
                    error_message=f"payload_keys={sorted(payload.keys())}",
                )
                if args.dry_run:
                    # Batch 38 (#47): dry-run no longer increments
                    # success eagerly. We log the preview above, but
                    # actual success only after a real call returns a
                    # record_id (in live mode). Keep dry-run counted as
                    # skipped so the audit doesn't claim live successes.
                    print(f"[stage 8] DRY-RUN: would upsert company "
                          f"{f.fund_id} with {len(payload)} attrs "
                          f"(keys={sorted(payload.keys())})")
                    run.skipped += 1
                    continue
                result = client.upsert_record(
                    fund_object, matching[fund_object], payload
                )
                attio_id = (result.get("data") or {}).get("id", {}).get("record_id")
                # Findings 29 + 45: a 2xx without a returned record_id is
                # NOT a success. Refuse to claim it.
                if not attio_id:
                    log_sync(
                        engine, object_type="company", local_id=f.fund_id,
                        attio_record_id=None, operation="upsert",
                        success=False,
                        error_message="Attio returned 2xx with no record_id",
                    )
                    run.failed += 1
                    run.log_error(
                        f.fund_id, "no_record_id",
                        "Attio upsert returned 2xx with no record_id",
                    )
                    continue
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
                    partners.c.email.label("partner_email"),
                    partners.c.attio_record_id.label("known_attio_id"),
                )
                .join(partners, partners.c.partner_id == partner_score_summaries.c.partner_id)
                .order_by(partner_score_summaries.c.send_now_priority.desc())
                .limit(args.top)
            ))
            # Pull recommended email draft + followup + deck per partner.
            # Batch 11 (#477-480): order by draft_id / followup_id / response_id
            # ASCENDING so the last-iterated row wins, guaranteeing we sync
            # the LATEST recommended/alternate per partner. Previously the
            # select had no ORDER BY -- DB-iteration order could surface a
            # stale recommended draft from an old Stage 7 run.
            drafts_by_partner: dict[str, dict] = {}
            for d in conn.execute(
                select(email_drafts).order_by(email_drafts.c.draft_id.asc())
            ):
                key = "recommended" if d.is_recommended else "alternate"
                drafts_by_partner.setdefault(d.partner_id, {})[key] = d
            followups_by_partner: dict[str, str] = {}
            for f in conn.execute(
                select(followup_drafts).order_by(
                    followup_drafts.c.followup_id.asc()
                )
            ):
                followups_by_partner[f.partner_id] = f.body
            deck_by_partner: dict[str, str] = {}
            for d in conn.execute(
                select(deck_request_responses).order_by(
                    deck_request_responses.c.response_id.asc()
                )
            ):
                deck_by_partner[d.partner_id] = d.body

        for p in partner_rows:
            run.processed += 1
            try:
                drafts = drafts_by_partner.get(p.partner_id, {})
                # Findings 42, 43: --require-ready-to-send only syncs
                # partners whose recommended draft passed Stage 7 QA AND
                # whose recommended_to_send is True. Prevents pushing
                # known-bad drafts as ready-to-send into Attio.
                if args.require_ready_to_send:
                    rec_draft = drafts.get("recommended")
                    if not p.recommended_to_send:
                        run.skipped += 1
                        continue
                    if rec_draft is None or rec_draft.qa_status != "pass":
                        log_sync(
                            engine, object_type="person",
                            local_id=p.partner_id, attio_record_id=None,
                            operation="skip_qa_fail", success=False,
                            error_message=(
                                "draft qa_status != 'pass'; --require-ready-to-send "
                                "refused to sync"
                            ),
                        )
                        run.skipped += 1
                        continue
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

                # Match cascade for an existing Attio record:
                #   0. partners.attio_record_id (from a prior sync) -> GET it
                #      directly. This is the strongest signal -- if the
                #      cascade below fails (LinkedIn URL changed, name typo,
                #      company link broken), we'd otherwise create a
                #      duplicate. Finding #1 fix.
                #   1. email
                #   2. linkedin_url query
                #   3. name + company-link query
                match = None
                op = None
                if p.known_attio_id:
                    try:
                        rec = client.get_record(person_object, p.known_attio_id)
                        if rec:
                            match = rec
                    except AttioError as exc:
                        # Stale local id -- record may have been deleted in
                        # Attio. Clear the local link and fall through to
                        # the cascade.
                        log_sync(
                            engine, object_type="person",
                            local_id=p.partner_id,
                            attio_record_id=p.known_attio_id,
                            operation="known_id_stale", success=False,
                            error_message=str(exc),
                        )
                        with engine.begin() as conn:
                            conn.execute(
                                partners.update()
                                .where(partners.c.partner_id == p.partner_id)
                                .values(attio_record_id=None)
                            )
                if match is None:
                    match = find_partner_record(
                        client, person_object,
                        email=p.partner_email,
                        linkedin_url=p.linkedin_url,
                        name=p.partner_name,
                        company_record_id=fund_attio_id,
                    )
                attio_id = None
                if match and match.get("_conflict"):
                    # Finding 37, 44: multiple matches is a real audit
                    # event, not just a skip. Count as failure so the
                    # process exits non-zero AND surface prominently.
                    cand_count = len(match['candidates'])
                    err_msg = (
                        f"{cand_count} candidate matches for {p.partner_name!r} "
                        f"at {fund_attio_id!r}; refusing to create a duplicate"
                    )
                    print(f"[stage 8] CONFLICT: {p.partner_id}: {err_msg}")
                    log_sync(engine, object_type="person", local_id=p.partner_id,
                             attio_record_id=None, operation="skip_conflict",
                             success=False, error_message=err_msg)
                    run.failed += 1
                    run.log_error(p.partner_id, "person_conflict", err_msg)
                    continue
                if match:
                    existing_state = existing_partner_state(match)
                    payload, removed = strip_preserved_fields(
                        payload, existing_state, attio_cfg, partner_attr_map
                    )
                    # Finding 38: log what got stripped so the operator can
                    # audit "I patched, but these fields stayed alone".
                    if removed:
                        log_sync(
                            engine, object_type="person",
                            local_id=p.partner_id,
                            attio_record_id=match.get("id", {}).get("record_id"),
                            operation="preserve_stripped", success=True,
                            error_message=(
                                f"preserved on-outreach-started: {sorted(removed)}"
                            ),
                        )
                    attio_id = match.get("id", {}).get("record_id")
                    op = "patch"
                    if args.dry_run:
                        print(f"[stage 8] DRY-RUN: would PATCH person "
                              f"{p.partner_id} attio_id={attio_id} "
                              f"with {len(payload)} attrs "
                              f"(preserved: {sorted(removed)})")
                        run.succeeded += 1
                        continue
                    if attio_id and payload:
                        client.update_record(person_object, attio_id, payload)
                    elif attio_id and not payload:
                        # Batch 12 (#383): an empty payload is a NO-OP, not a
                        # silent success. Log it so the operator can see that
                        # nothing was sent and the preserved-fields logic is
                        # working as intended (or, conversely, that something
                        # else upstream blanked the payload).
                        log_sync(
                            engine, object_type="person",
                            local_id=p.partner_id,
                            attio_record_id=attio_id,
                            operation="patch_noop", success=True,
                            error_message=(
                                "all writable fields preserved or empty; "
                                "no remote PATCH issued"
                            ),
                        )
                else:
                    if args.dry_run:
                        print(f"[stage 8] DRY-RUN: would CREATE person "
                              f"{p.partner_id} with {len(payload)} attrs")
                        run.succeeded += 1
                        continue
                    result = client.create_record(person_object, payload)
                    attio_id = (result.get("data") or {}).get("id", {}).get("record_id")
                    op = "create"
                # Findings 29 + 45: don't claim success when Attio
                # returned 2xx with no record_id.
                if not attio_id:
                    log_sync(
                        engine, object_type="person", local_id=p.partner_id,
                        attio_record_id=None, operation=op or "?",
                        success=False,
                        error_message="Attio returned 2xx with no record_id",
                    )
                    run.failed += 1
                    run.log_error(
                        p.partner_id, "no_record_id",
                        f"Attio {op} returned 2xx with no record_id",
                    )
                    continue

                if attio_id:
                    now = _now()
                    with engine.begin() as conn:
                        conn.execute(
                            partners.update().where(
                                partners.c.partner_id == p.partner_id
                            ).values(
                                attio_record_id=attio_id, last_updated=now,
                            )
                        )
                        # Batch 12 (#379/#380/#381): record pushed_to_attio_at
                        # on the LATEST recommended/alternate draft + the
                        # latest followup + the latest deck-response for this
                        # partner so the operator (and `status.py`) can see
                        # exactly which artifact was synced and when. Without
                        # this the timestamp column stayed NULL forever.
                        rec_draft = drafts.get("recommended")
                        if rec_draft is not None:
                            conn.execute(
                                email_drafts.update()
                                .where(email_drafts.c.draft_id == rec_draft.draft_id)
                                .values(pushed_to_attio_at=now)
                            )
                        alt_draft = drafts.get("alternate")
                        if alt_draft is not None:
                            conn.execute(
                                email_drafts.update()
                                .where(email_drafts.c.draft_id == alt_draft.draft_id)
                                .values(pushed_to_attio_at=now)
                            )
                        latest_followup_id = conn.execute(
                            select(followup_drafts.c.followup_id)
                            .where(followup_drafts.c.partner_id == p.partner_id)
                            .order_by(followup_drafts.c.followup_id.desc())
                            .limit(1)
                        ).scalar()
                        if latest_followup_id is not None:
                            conn.execute(
                                followup_drafts.update()
                                .where(followup_drafts.c.followup_id == latest_followup_id)
                                .values(pushed_to_attio_at=now)
                            )
                        latest_deck_id = conn.execute(
                            select(deck_request_responses.c.response_id)
                            .where(deck_request_responses.c.partner_id == p.partner_id)
                            .order_by(deck_request_responses.c.response_id.desc())
                            .limit(1)
                        ).scalar()
                        if latest_deck_id is not None:
                            conn.execute(
                                deck_request_responses.update()
                                .where(deck_request_responses.c.response_id == latest_deck_id)
                                .values(pushed_to_attio_at=now)
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

        # Batch 12 (#385): close the Attio client even on unexpected
        # exception paths. Previously a crash between "begin partners loop"
        # and the unconditional client.close() leaked the underlying httpx
        # session. RunLogger's __exit__ still records the failed run.
        try:
            # Batch 38 (#54): surface preserve-stripped counts in the
            # summary so the operator can see how often the preserve-
            # on-outreach-started logic kicked in this run. We scope to
            # "rows landed since this run started" by joining against
            # the current run_id; without a per-run column on
            # attio_sync_log, fall back to counting events whose
            # synced_at is >= the latest runs.started_at for this stage.
            with engine.begin() as conn:
                this_run_start = conn.execute(
                    select(runs.c.started_at).where(
                        runs.c.run_id == run.run_id,
                    )
                ).scalar()
                preserved_q = (
                    select(func.count()).select_from(attio_sync_log)
                    .where(attio_sync_log.c.operation == "preserve_stripped")
                )
                if this_run_start is not None:
                    preserved_q = preserved_q.where(
                        attio_sync_log.c.synced_at >= this_run_start,
                    )
                preserved_count = conn.execute(preserved_q).scalar() or 0
            print(
                f"[stage 8] synced {run.succeeded} record(s); "
                f"failed={run.failed} skipped={run.skipped} "
                f"preserve_stripped_events={preserved_count}"
            )
            if preserved_count:
                run.note(
                    f"preserve_stripped fired {preserved_count} time(s) "
                    f"this run"
                )
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - close shouldn't mask the real error
                pass
        # Finding 7: automation must see partial sync failure as red, not green.
        if run.failed:
            return 2

    return 0


def _lookup_fund_attio_id(engine, fund_id: str) -> str | None:
    with engine.begin() as conn:
        row = conn.execute(
            select(funds.c.attio_record_id).where(funds.c.fund_id == fund_id)
        ).first()
    return row.attio_record_id if row else None


if __name__ == "__main__":
    raise SystemExit(main())
