"""Discovery surface (Phase 4): match shared `investors_global`
rows to the current tenant's company profile + let them claim
matches into their per-workspace `partners` / `funds` tables.

Two operations:

  - `find_matches(workspace_engine, global_engine, company_cfg,
    limit) -> list[Match]`
      Reads investors_global, filters out anything the tenant
      already has in `partners` (by firm+partner string match),
      ranks by simple fit-to-company-profile heuristics, returns
      the top N.

  - `claim_investor(workspace_engine, global_engine, global_id) ->
    ClaimResult`
      Reads one investors_global row, upserts a `funds` row for
      the firm + a `partners` row for the partner, and stamps
      `partners.claimed_from_global_id` so the audit trail
      survives.

Fit heuristics (intentionally simple for v1):
  - +3 per overlapping sector
  - +2 per overlapping stage / geography
  - +1 baseline so an investor with NO criteria still appears at the bottom
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.company_config import CompanyConfig
from core.db import funds, partners
from core.ids import fund_id_for, normalize_domain, partner_id_for
from core.investors_global import investors_global


@dataclass(frozen=True)
class Match:
    """One ranked discovery result. The fields are flat so the
    frontend can render them directly without an extra JOIN."""
    global_id: int
    firm: str
    partner: str
    email: str | None
    stages: list[str]
    sectors: list[str]
    geographies: list[str]
    enriched_fields: dict
    fit_score: int
    fit_reasons: list[str]


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of `claim_investor`. The fund + partner ids are
    canonical (slug-based), so the frontend can deep-link to
    `/partners/{partner_id}/...` after a claim."""
    fund_id: str
    partner_id: str
    global_id: int
    created_fund: bool
    created_partner: bool


# ---------- internals ----------

def _decode_array(raw: str | None) -> list[str]:
    """investors_global stores arrays as JSON strings. Defensive
    decoder: bad JSON / non-list payloads collapse to []."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _decode_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def _tenant_known_keys(engine: Engine) -> set[tuple[str, str]]:
    """Return (firm.lower(), partner.lower()) for every partner the
    tenant already has -- used to filter discovery results so we
    don't suggest a fund/partner they already uploaded.

    The match key mirrors investors_global's dedup: case-insensitive
    firm+partner. Email matches aren't checked here because the
    tenant's partner table doesn't always carry email at discovery
    time (Apollo enrichment may arrive later).
    """
    known: set[tuple[str, str]] = set()
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(
                funds.c.fund_id, funds.c.name,
                partners.c.partner_id, partners.c.name,
            ).select_from(
                partners.join(funds, partners.c.fund_id == funds.c.fund_id)
            )
        ))
    for r in rows:
        fund_name = r[1] or ""
        partner_name = r[3] or ""
        if fund_name and partner_name:
            known.add((fund_name.strip().lower(),
                       partner_name.strip().lower()))
    return known


def _score_match(
    cfg: CompanyConfig, sectors: list[str], stages: list[str],
    geographies: list[str],
) -> tuple[int, list[str]]:
    """Return (fit_score, reasons). Reasons surface in the frontend
    so the operator sees WHY a match scored high."""
    # Lowercase everything once for case-insensitive overlap.
    inv_sectors = {s.strip().lower() for s in sectors if s}
    inv_stages = {s.strip().lower() for s in stages if s}
    inv_geos = {g.strip().lower() for g in geographies if g}

    target_sectors = {s.strip().lower() for s in cfg.company.target_sectors if s}
    target_geos = {g.strip().lower() for g in cfg.company.target_geographies if g}
    target_stages = {s.strip().lower() for s in cfg.company.target_stages if s}
    # The company's own stage (singular) ALSO counts as a target
    # when the explicit list is empty.
    if not target_stages and cfg.company.stage:
        target_stages = {cfg.company.stage.strip().lower()}

    score = 0
    reasons: list[str] = []

    sector_overlap = inv_sectors & target_sectors
    if sector_overlap:
        score += 3 * len(sector_overlap)
        reasons.append(
            f"sector overlap: {', '.join(sorted(sector_overlap))}"
        )

    stage_overlap = inv_stages & target_stages
    if stage_overlap:
        score += 2 * len(stage_overlap)
        reasons.append(
            f"stage overlap: {', '.join(sorted(stage_overlap))}"
        )

    geo_overlap = inv_geos & target_geos
    if geo_overlap:
        score += 2 * len(geo_overlap)
        reasons.append(
            f"geography overlap: {', '.join(sorted(geo_overlap))}"
        )

    # Baseline 1 so investors with no array overlap still appear
    # at the bottom of the ranking -- the operator might claim
    # them on partner-name signal rather than tagged criteria.
    if score == 0:
        score = 1
        reasons.append("no tagged-criteria overlap")
    return score, reasons


# ---------- public API ----------

def find_matches(
    workspace_engine: Engine,
    global_engine: Engine,
    company_cfg: dict | None,
    *, limit: int = 50,
) -> list[Match]:
    """Top-N investors_global rows NOT already in the tenant's
    partners table, ranked by fit_score descending then by
    last_enriched_at descending (newer enrichment wins ties).

    Empty company profile + empty pool -> empty list (no error).
    """
    cfg = CompanyConfig.from_dict(company_cfg)
    known = _tenant_known_keys(workspace_engine)

    matches: list[Match] = []
    with global_engine.begin() as conn:
        rows = list(conn.execute(
            select(investors_global).order_by(
                investors_global.c.last_enriched_at.desc(),
            )
        ))
    for row in rows:
        firm = (row.firm or "").strip()
        partner = (row.partner or "").strip()
        if not firm:
            continue
        key = (firm.lower(), partner.lower())
        if key in known:
            continue
        sectors = _decode_array(row.sectors)
        stages = _decode_array(row.stages)
        geos = _decode_array(row.geographies)
        score, reasons = _score_match(cfg, sectors, stages, geos)
        matches.append(Match(
            global_id=int(row.id),
            firm=firm, partner=partner,
            email=row.email,
            stages=stages, sectors=sectors, geographies=geos,
            enriched_fields=_decode_dict(row.enriched_fields),
            fit_score=score, fit_reasons=reasons,
        ))
    matches.sort(key=lambda m: (-m.fit_score, -m.global_id))
    return matches[:max(0, limit)]


class ClaimError(RuntimeError):
    """Raised when claim_investor can't fulfill the request --
    e.g. global_id doesn't exist. Caller surfaces as 404 / 400."""


def claim_investor(
    workspace_engine: Engine,
    global_engine: Engine,
    global_id: int,
) -> ClaimResult:
    """Copy one investors_global row into the tenant's funds +
    partners. Idempotent: a second claim of the same global_id
    finds the existing funds/partners rows and returns them.

    The funds row's `domain` comes from `enriched_fields["domain"]`
    if present, else from the email host, else a slug-from-firm
    placeholder. fund_id is the canonical `fund_id_for(domain)`.
    partner_id is `partner_id_for(domain, partner_name)`.
    """
    with global_engine.begin() as conn:
        row = conn.execute(
            select(investors_global).where(
                investors_global.c.id == global_id,
            ).limit(1)
        ).first()
    if row is None:
        raise ClaimError(
            f"investors_global row {global_id} not found"
        )

    firm = (row.firm or "").strip()
    partner_name = (row.partner or "").strip()
    if not firm or not partner_name:
        raise ClaimError(
            f"investors_global row {global_id} has empty firm or "
            f"partner; refusing to claim"
        )

    enriched = _decode_dict(row.enriched_fields)
    domain = normalize_domain(
        str(enriched.get("domain") or "")
        or _email_domain(row.email or "")
        or _slug_domain(firm)
    )
    if not domain:
        raise ClaimError(
            f"could not derive a domain for firm {firm!r}; "
            f"investors_global needs an enriched_fields.domain or "
            f"an email"
        )
    # Review item #12: a `.unclaimed` slug means we had no real
    # domain. Mark the fund provisional + do_not_contact the
    # partner so Stage 6 de-emphasizes it AND Stage 7 refuses to
    # generate an email -- the operator must edit the funds row
    # with a real domain before outreach goes out. Stage 2
    # enrichment can still try to scrape (it will 404 cleanly).
    is_pseudo_domain = domain.endswith(".unclaimed")
    fund_id = fund_id_for(domain)
    partner_id = partner_id_for(domain, partner_name)
    now = datetime.now(timezone.utc)

    created_fund = False
    created_partner = False
    with workspace_engine.begin() as conn:
        existing_fund = conn.execute(
            select(funds.c.fund_id).where(
                funds.c.fund_id == fund_id,
            )
        ).first()
        if existing_fund is None:
            conn.execute(funds.insert().values(
                fund_id=fund_id,
                name=firm,
                domain=domain,
                stated_thesis=str(enriched.get("thesis") or "") or None,
                stated_stage_focus=str(
                    enriched.get("stage_focus") or ""
                ) or None,
                check_size_range=str(
                    enriched.get("check_size_range") or ""
                ) or None,
                is_active=True,
                # is_provisional flags this fund as "needs operator
                # follow-up" -- see #12.
                is_provisional=is_pseudo_domain,
                last_updated=now,
            ))
            created_fund = True

        existing_partner = conn.execute(
            select(partners.c.partner_id).where(
                partners.c.partner_id == partner_id,
            )
        ).first()
        if existing_partner is None:
            conn.execute(partners.insert().values(
                partner_id=partner_id,
                fund_id=fund_id,
                name=partner_name,
                bio=str(enriched.get("bio") or "") or None,
                # Stamp the audit trail back to the source row in
                # the discovery pool.
                claimed_from_global_id=int(global_id),
                # #12: also stamp do_not_contact when we have only
                # a pseudo-domain. Stage 7 refuses to draft cold
                # outreach to do_not_contact partners; this stops
                # the email going out before the operator has
                # fixed the domain by hand.
                do_not_contact=is_pseudo_domain,
                do_not_contact_reason=(
                    "claimed without a real domain "
                    "(.unclaimed slug); edit the fund's domain "
                    "before contacting"
                ) if is_pseudo_domain else None,
                do_not_contact_source=(
                    "discovery_claim_pseudo_domain"
                    if is_pseudo_domain else None
                ),
                do_not_contact_set_at=now if is_pseudo_domain else None,
                is_provisional=is_pseudo_domain,
                last_updated=now,
            ))
            created_partner = True
        else:
            # The partner already exists locally (e.g. via Stage 2)
            # but the operator chose to claim again -- backfill
            # the link.
            from sqlalchemy import update
            conn.execute(
                update(partners)
                .where(partners.c.partner_id == partner_id)
                .values(claimed_from_global_id=int(global_id))
            )

    return ClaimResult(
        fund_id=fund_id, partner_id=partner_id,
        global_id=int(global_id),
        created_fund=created_fund,
        created_partner=created_partner,
    )


def _email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[1].strip()


def _slug_domain(firm: str) -> str:
    """Last-resort domain for a firm with no email + no enriched
    domain. Produces a stable slug-shaped pseudo-domain that
    sorts uniquely; the operator can edit the funds row later."""
    cleaned = "".join(
        ch if ch.isalnum() else "-"
        for ch in (firm or "").strip().lower()
    ).strip("-")
    return f"{cleaned}.unclaimed" if cleaned else ""
