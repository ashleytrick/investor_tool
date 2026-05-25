"""Provisional-promotion + bulk re-attribution (Slice 12).

Closes the loop on Stage 3's `--allow-provisional` path:

- Stage 3 creates a `is_provisional=TRUE` fund (or partner) when the LLM
  names something the local DB doesn't know about. The row is usable
  for attribution but flagged as not-yet-confirmed by a human.
- This module exposes the operator-side counterpart: confirm a
  provisional row (clear the flag, optionally fill in name/domain
  details), OR merge a provisional fund into a real one (via
  `bulk_reattribute_deals` then `delete_fund`).

Pure-function shape so the operator CLI is a thin wrapper and the
unit tests don't need subprocess plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from core.attribution.status import (
    MATCHED_BY_MANUAL, STATUS_CONFIRMED,
)
from core.db import deal_attributions, funds, partners


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PromotionError(Exception):
    """Operator-input error: row missing, wrong state, conflict."""


@dataclass
class FundPromotionResult:
    fund_id: str
    cleared_provisional: bool
    renamed_to: str | None
    domain_set_to: str | None


@dataclass
class PartnerPromotionResult:
    partner_id: str
    cleared_provisional: bool
    renamed_to: str | None


@dataclass
class ReattributionResult:
    from_fund_id: str
    to_fund_id: str
    deals_moved: int
    partners_remapped: int
    partners_orphaned: list[str] = field(default_factory=list)
    dry_run: bool = False


def promote_provisional_fund(
    engine: Any,
    *,
    fund_id: str,
    new_name: str | None = None,
    new_domain: str | None = None,
) -> FundPromotionResult:
    """Clear `is_provisional=TRUE` on a fund row.

    `new_name` / `new_domain` are optional text overrides for the
    operator-facing labels. The internal `fund_id` is not rewritten
    (changing it would orphan every deal_attributions row pointing
    here); use `bulk_reattribute_deals` if the operator wants to
    merge a provisional fund into a different real fund.

    Raises `PromotionError` when the fund doesn't exist or wasn't
    actually provisional.
    """
    with engine.begin() as conn:
        row = conn.execute(
            select(funds.c.fund_id, funds.c.is_provisional, funds.c.name)
            .where(funds.c.fund_id == fund_id)
        ).first()
        if row is None:
            raise PromotionError(f"fund_id={fund_id!r} not found")
        if not row.is_provisional:
            raise PromotionError(
                f"fund_id={fund_id!r} already non-provisional (name={row.name!r})"
            )
        values: dict[str, Any] = {
            "is_provisional": False,
            "last_updated": _now(),
        }
        if new_name is not None:
            values["name"] = new_name
        if new_domain is not None:
            values["domain"] = new_domain
        conn.execute(
            update(funds).where(funds.c.fund_id == fund_id).values(**values)
        )
    return FundPromotionResult(
        fund_id=fund_id,
        cleared_provisional=True,
        renamed_to=new_name,
        domain_set_to=new_domain,
    )


def promote_provisional_partner(
    engine: Any,
    *,
    partner_id: str,
    new_name: str | None = None,
    new_title: str | None = None,
    new_linkedin: str | None = None,
) -> PartnerPromotionResult:
    """Clear `is_provisional=TRUE` on a partner row.

    Same shape as `promote_provisional_fund`: optional text overrides
    for downstream display, `partner_id` itself is not rewritten.
    """
    with engine.begin() as conn:
        row = conn.execute(
            select(partners.c.partner_id, partners.c.is_provisional, partners.c.name)
            .where(partners.c.partner_id == partner_id)
        ).first()
        if row is None:
            raise PromotionError(f"partner_id={partner_id!r} not found")
        if not row.is_provisional:
            raise PromotionError(
                f"partner_id={partner_id!r} already non-provisional "
                f"(name={row.name!r})"
            )
        values: dict[str, Any] = {
            "is_provisional": False,
            "last_updated": _now(),
        }
        if new_name is not None:
            values["name"] = new_name
        if new_title is not None:
            values["title"] = new_title
        if new_linkedin is not None:
            values["linkedin_url"] = new_linkedin
        conn.execute(
            update(partners).where(partners.c.partner_id == partner_id)
            .values(**values)
        )
    return PartnerPromotionResult(
        partner_id=partner_id,
        cleared_provisional=True,
        renamed_to=new_name,
    )


def _normalize(name: str | None) -> str:
    return (name or "").strip().lower()


def bulk_reattribute_deals(
    engine: Any,
    *,
    from_fund_id: str,
    to_fund_id: str,
    actor: str,
    also_remap_partners: bool = False,
    dry_run: bool = False,
) -> ReattributionResult:
    """Move every `deal_attributions` row where lead_fund_id=from_fund_id
    to point at `to_fund_id`.

    `match_status` is set to `confirmed` and `matched_by` to `manual`
    on every moved row (the operator is the authoritative signal).

    When `also_remap_partners` is set and a moved row has a non-null
    `attributed_partner_id`, the partner is looked up by name in the
    destination fund and the attribution rewritten; partners that can't
    be matched land in `partners_orphaned` and the row's
    `attributed_partner_id` is cleared (so Stage 6 doesn't credit the
    wrong fund's partner with the deal).

    `dry_run=True` short-circuits before any write and reports the
    counts the real call would produce.

    Raises `PromotionError` for missing funds or the no-op same-id
    case (operator typo).
    """
    if from_fund_id == to_fund_id:
        raise PromotionError(
            f"from_fund_id and to_fund_id are the same ({from_fund_id!r})"
        )

    with engine.begin() as conn:
        src = conn.execute(
            select(funds.c.fund_id, funds.c.name)
            .where(funds.c.fund_id == from_fund_id)
        ).first()
        dst = conn.execute(
            select(funds.c.fund_id, funds.c.name)
            .where(funds.c.fund_id == to_fund_id)
        ).first()
        if src is None:
            raise PromotionError(f"from_fund_id={from_fund_id!r} not found")
        if dst is None:
            raise PromotionError(f"to_fund_id={to_fund_id!r} not found")

        deals = list(conn.execute(
            select(
                deal_attributions.c.deal_id,
                deal_attributions.c.attributed_partner_id,
            ).where(deal_attributions.c.lead_fund_id == from_fund_id)
        ))

        # Build remap table only if needed: partner name -> partner_id
        # within the destination fund.
        name_to_dest_partner: dict[str, str] = {}
        if also_remap_partners and deals:
            for r in conn.execute(
                select(partners.c.partner_id, partners.c.name)
                .where(partners.c.fund_id == to_fund_id)
            ):
                name_to_dest_partner[_normalize(r.name)] = r.partner_id

        orphaned: list[str] = []
        remapped = 0
        moves: list[tuple[int, str | None, str | None]] = []
        for d in deals:
            new_partner_id: str | None = d.attributed_partner_id
            partner_change = None
            if also_remap_partners and d.attributed_partner_id:
                src_partner = conn.execute(
                    select(partners.c.name)
                    .where(partners.c.partner_id == d.attributed_partner_id)
                ).first()
                lookup = _normalize(src_partner.name) if src_partner else ""
                hit = name_to_dest_partner.get(lookup) if lookup else None
                if hit:
                    new_partner_id = hit
                    if hit != d.attributed_partner_id:
                        remapped += 1
                        partner_change = hit
                else:
                    orphaned.append(d.attributed_partner_id)
                    new_partner_id = None
                    partner_change = "ORPHAN"
            moves.append((d.deal_id, new_partner_id, partner_change))

        if dry_run:
            return ReattributionResult(
                from_fund_id=from_fund_id,
                to_fund_id=to_fund_id,
                deals_moved=len(deals),
                partners_remapped=remapped,
                partners_orphaned=orphaned,
                dry_run=True,
            )

        now = _now()
        for deal_id, new_partner_id, _change in moves:
            conn.execute(
                update(deal_attributions)
                .where(deal_attributions.c.deal_id == deal_id)
                .values(
                    lead_fund_id=to_fund_id,
                    attributed_partner_id=new_partner_id,
                    match_status=STATUS_CONFIRMED,
                    matched_by=MATCHED_BY_MANUAL,
                    review_status=STATUS_CONFIRMED,
                    reviewed_by=actor,
                    reviewed_at=now,
                )
            )

    return ReattributionResult(
        from_fund_id=from_fund_id,
        to_fund_id=to_fund_id,
        deals_moved=len(moves),
        partners_remapped=remapped,
        partners_orphaned=orphaned,
        dry_run=False,
    )
