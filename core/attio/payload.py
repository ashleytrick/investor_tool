"""Attio payload builders (Refactor item 7 / 15).

Pure functions that translate local DB rows into Attio v2 PATCH /
CREATE / UPSERT request bodies. Split out of Stage 8 so the payload
assembly is testable independently of the AttioClient -- previously
every payload-shape regression required a full Stage 8 subprocess
run.

The Attio v2 attribute shape is a list-of-dicts per slug:
  - select fields  : [{"option": {"title": "..."}}]
  - everything else: [{"value": ...}]
plus two special slugs that take their own object form:
  - "domains"      : [{"domain": "company.io"}]
  - record refs    : [{"target_object": "...", "target_record_id": "..."}]
"""
from __future__ import annotations

from typing import Any


# Attribute slugs Attio represents as single-select. Values for these
# slugs must be wrapped as [{"option": {"title": "..."}}]; everything
# else uses [{"value": ...}].
SELECT_SLUGS: frozenset[str] = frozenset({
    "stage_focus", "score_confidence", "email_strategy_used",
    "email_alternate_strategy", "template_smell", "outreach_status",
    "meeting_outcome", "reply_type",
})


def wrap_value(value: Any, api_slug: str) -> list[dict] | None:
    """Convert a raw DB value to the Attio v2 list-of-dicts shape.

    Returns None for None / empty-string so callers can drop the
    field entirely rather than send a NULL (which Attio interprets
    as "clear this field").
    """
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


# Back-compat alias used by scripts/08 during the extraction window.
_wrap_value = wrap_value


def build_payload(attr_map: dict[str, str], source: dict) -> dict:
    """For each (db_key, api_slug) pair in attr_map, pull source[db_key]
    and wrap it. Drops fields where wrap_value returned None.
    """
    payload: dict = {}
    for db_key, api_slug in attr_map.items():
        wrapped = wrap_value(source.get(db_key), api_slug)
        if wrapped is not None:
            payload[api_slug] = wrapped
    return payload


def build_fund_payload(
    *,
    fund_name: str,
    fund_domain: str | None,
    fund_source: dict,
    attr_map: dict[str, str],
) -> dict:
    """Compose the full PATCH/UPSERT body for a fund-as-company write.

    Combines:
      - base fields (Attio company identity): `name`, `domains`
      - custom fields driven by attr_map (everything in attio.yaml's
        fund_attributes block)

    `fund_domain` is dropped from `domains` when None / empty so Attio
    doesn't interpret it as a NULL clear.
    """
    base: dict = {"name": [{"value": fund_name}]}
    if fund_domain:
        base["domains"] = [{"domain": fund_domain}]
    custom = build_payload(attr_map, fund_source)
    return {**base, **custom}


def build_partner_payload(
    *,
    partner_name: str,
    partner_source: dict,
    attr_map: dict[str, str],
    fund_object: str,
    fund_attio_id: str | None,
) -> dict:
    """Compose the full PATCH/CREATE body for a partner-as-person write.

    Combines:
      - base fields: `name`, and a `company` ref when we know the
        Attio record_id for the partner's fund;
      - custom fields driven by attr_map (everything in attio.yaml's
        partner_attributes block including the email/linkedin/etc).

    `fund_attio_id` is omitted when None -- callers may PATCH a partner
    without (re)attaching the company link if the fund hasn't been
    synced yet this run.
    """
    base: dict = {"name": [{"value": partner_name}]}
    if fund_attio_id:
        base["company"] = [{
            "target_object": fund_object,
            "target_record_id": fund_attio_id,
        }]
    custom = build_payload(attr_map, partner_source)
    return {**base, **custom}
