"""Stage 2 fund-enrichment reconciliation (Refactor item 7 / 11).

Pure helpers Stage 2 uses to apply an LLM-derived FundEnrichment to
the local DB:

  - build_fund_update_values(enrichment, now) -> dict
      Preserve-on-empty fund row builder. Only fields the LLM
      actually filled this run land in the dict; missing fields stay
      at their existing DB value (Batch 11 #412/#413). Otherwise a
      sparse re-run -- LLM missed a field, site changed -- would
      silently blank out richer prior enrichment.

  - partner_upsert_values(fund_id, fund_domain, partner, now) -> dict
      Per-partner row shaper. Computes the deterministic partner_id
      slug + sets employment_status='likely_current' for any
      partner present on the team page this run.

  - compute_vanished_partners(prior_pids, discovered_pids) -> list[str]
      Returns the partner_ids that were on this fund's roster last
      run but are no longer on the team page. Caller demotes them to
      employment_status='uncertain' so a partner who left a fund
      doesn't satisfy Stage 6 criterion 6 indefinitely.

This module is pure: it consumes the FundEnrichment Pydantic instance
and primitive values, and returns plain dicts / lists. No DB, no LLM.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from core.ids import partner_id_for


# Fields on the FundEnrichment schema that are subject to preserve-
# on-empty semantics. Each tuple is (enrichment_attr, db_column,
# transform_fn). transform_fn formats the enrichment value into the
# string the DB column stores; it's None for verbatim copies.
_PRESERVE_ON_EMPTY: tuple[
    tuple[str, str, Any], ...
] = (
    ("thesis_summary", "stated_thesis", None),
    ("stated_stage_focus", "stated_stage_focus", None),
    ("check_size_range", "check_size_range", None),
    ("explicit_kill_signals", "kill_signals",
     lambda lst: "; ".join(lst) if lst else None),
)


def build_fund_update_values(
    enrichment: Any, now: datetime, *, conn: Any = None,
) -> dict:
    """Return a dict of column -> value to apply to the funds row.

    `last_updated`, `source_urls` (legacy delimited), and -- when
    `conn` is supplied -- `source_ids` (JSON list of source_id values
    into the canonical `sources` registry, Slice 18b follow-up #18)
    always land. Every other field is preserve-on-empty: only included
    when the LLM filled it this run, so a sparse re-run can't blank
    out a richer prior extraction.

    `conn` is optional for backward compatibility with callers that
    pre-date the sources registry. When omitted, `source_ids` is left
    NULL and m004_backfill_funds_source_ids picks it up on next
    workspace open.
    """
    import json as _json

    out: dict[str, Any] = {
        "last_updated": now,
        "source_urls": "; ".join(str(u) for u in enrichment.source_urls_used),
    }
    if conn is not None:
        from core.sources import upsert_source
        sids: list[int] = []
        for url in enrichment.source_urls_used:
            sid = upsert_source(
                conn, source_url=str(url), source_type="fund_team_page",
            )
            if sid not in sids:
                sids.append(sid)
        out["source_ids"] = _json.dumps(sids)
    for attr, col, transform in _PRESERVE_ON_EMPTY:
        value = getattr(enrichment, attr, None)
        if not value:
            continue  # preserve-on-empty -- skip when LLM produced nothing
        out[col] = transform(value) if transform else value
    return out


def partner_upsert_values(
    *,
    fund_id: str,
    fund_domain: str,
    partner: Any,
    now: datetime,
) -> dict:
    """Shape one team-page partner into a partners row upsert dict.

    employment_status='likely_current' per the brief's ladder for a
    single-source recent observation; LinkedIn cross-check (->
    verified_current) and departure feeds (-> left_fund) are future
    enhancements.

    `partner` is a Pydantic model with .name / .title / .bio_snippet.
    """
    return {
        "partner_id": partner_id_for(fund_domain, partner.name),
        "fund_id": fund_id,
        "name": partner.name,
        "title": partner.title,
        "bio": partner.bio_snippet,
        "employment_status": "likely_current",
        "last_updated": now,
    }


def compute_vanished_partners(
    prior_pids: Iterable[str],
    discovered_pids: Iterable[str],
) -> list[str]:
    """Return the partner_ids that were on this fund's roster previously
    but are no longer on the current team page.

    Returns an empty list when discovered_pids is empty -- a team
    page that produced zero partners this run is more likely an
    LLM extraction miss than a true mass-departure, so callers
    intentionally skip the demotion to avoid silently invalidating
    every partner under that fund.
    """
    discovered = set(discovered_pids)
    if not discovered:
        return []
    prior = set(prior_pids)
    return sorted(prior - discovered)
