"""Attio preserve / override behavior (Refactor item 7).

Stage 8 needs two rules around Attio writes:

  1. **Preserve-on-outreach-started.** Once a partner's
     outreach_status in Attio moves to a non-initial state (sent /
     replied / meeting_booked / passed_*), Stage 8 must not overwrite
     certain fields with a fresh local re-score -- those fields
     represent the operator's manual tracking state and shouldn't be
     blown away by a routine cron. The preserve_on_outreach_started
     config block lists the trigger statuses + the field set to
     leave alone.

  2. **Manual-override protection.** When the operator has marked
     manual_score_override (or manual_recommended_override) on the
     local row, the corresponding Attio fields must also be left
     alone. The manual_override_protection block lists which
     attribute groups stay frozen per flag.

Both rules end up applied to the OUTGOING PATCH payload: strip the
forbidden keys before sending. This module owns the rule application
so Stage 8 stays an orchestrator.
"""
from __future__ import annotations


def existing_partner_state(record: dict) -> dict:
    """Pull the fields we care about for preserve / override checks
    from an Attio person record. Returns a small dict with three
    keys: outreach_status (option title or None), and the two
    manual_*override booleans."""
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
        except Exception:  # noqa: BLE001 - tolerate any Attio shape drift
            return None

    return {
        "outreach_status": _option("outreach_status"),
        "manual_score_override": bool(_scalar("manual_score_override")),
        "manual_recommended_override": bool(
            _scalar("manual_recommended_override"),
        ),
    }


def strip_preserved_fields(
    payload: dict,
    existing: dict,
    attio_cfg: dict,
    attr_map: dict[str, str],
) -> tuple[dict, list[str]]:
    """Remove fields per preserve-on-outreach-started + manual-override
    rules. Returns the (possibly-trimmed) payload and a list of the
    api_slug values that were stripped, so the caller can log which
    fields stayed alone for audit.

    attr_map is the workspace's db-key -> Attio-attr-slug mapping
    (e.g. {"composite_fit_score": "composite_fit_score_v2"}).
    Falls back to the db_key when no mapping exists.

    The function mutates `payload` in place AND returns it, so call
    sites can do `payload, removed = strip_preserved_fields(...)`.
    """
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
        for db_key in mop.get(
            "if_manual_score_override_true_preserve",
        ) or []:
            api_slug = attr_map.get(db_key, db_key)
            if api_slug in payload:
                payload.pop(api_slug)
                removed.append(api_slug)
    if existing.get("manual_recommended_override"):
        for db_key in mop.get(
            "if_manual_recommended_override_true_preserve",
        ) or []:
            api_slug = attr_map.get(db_key, db_key)
            if api_slug in payload:
                payload.pop(api_slug)
                removed.append(api_slug)
    return payload, removed
