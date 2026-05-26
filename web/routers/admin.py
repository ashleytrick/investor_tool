"""Admin endpoints (Phase 5).

Three read-only endpoints gated by `require_admin`:

  GET /admin/companies  -- all tenants' company.yaml `company`
                           block + tenant identity
  GET /admin/investors  -- per-tenant partners JOIN funds, plus
                           the shared investors_global row when
                           the local partner was claimed from it
  GET /admin/tenants    -- summary roster: counts + last-active

Scatter-gather pattern: walk `${WORKSPACES_ROOT}` for tenant
directories, open each per-tenant SQLite, aggregate. This is the
'awkward admin queries' tradeoff we accepted for SQLite-per-
tenant isolation (single missed WHERE user_id = ? CAN'T leak
between tenants because they're separate files).
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime
from typing import Optional

import yaml  # type: ignore
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from core.db import (
    deal_attributions,
    email_drafts,
    funds,
    get_engine,
    partners,
    runs,
)
from core.investors_global import get_global_engine, investors_global
from web.deps import (
    _per_user_workspaces_enabled,
    _workspaces_root,
    require_admin,
)


router = APIRouter(tags=["admin"])


# ---------- response models ----------

class AdminCompany(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    name: Optional[str] = None
    one_liner: Optional[str] = None
    stage: Optional[str] = None
    sectors: list[str] = Field(default_factory=list)
    business_model: Optional[str] = None
    created_at: Optional[str] = None


class SkippedTenant(BaseModel):
    """Review item #22: surface per-tenant load failures so admins
    can see WHICH workspaces are broken and why, instead of admin
    endpoints silently dropping bad tenants from the result."""
    user_id: str
    error: str


class AdminCompaniesResult(BaseModel):
    companies: list[AdminCompany] = Field(default_factory=list)
    count: int = 0
    skipped: list[SkippedTenant] = Field(default_factory=list)


class AdminInvestor(BaseModel):
    user_id: str
    fund_id: str
    partner_id: str
    firm: str
    partner: str
    email: Optional[str] = None
    stages: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    claimed_from_global_id: Optional[int] = None
    global_enriched_fields: dict = Field(default_factory=dict)
    last_updated: Optional[str] = None


class AdminInvestorsResult(BaseModel):
    investors: list[AdminInvestor] = Field(default_factory=list)
    count: int = 0
    skipped: list[SkippedTenant] = Field(default_factory=list)


class AdminTenant(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    company_count: int = 0
    investor_count: int = 0
    draft_count: int = 0
    last_active_at: Optional[str] = None


class AdminTenantsResult(BaseModel):
    tenants: list[AdminTenant] = Field(default_factory=list)
    count: int = 0
    skipped: list[SkippedTenant] = Field(default_factory=list)


# ---------- helpers ----------

def _iter_tenant_workspaces() -> list[pathlib.Path]:
    """List every per-user workspace directory under
    `${WORKSPACES_ROOT}`. Empty list when the multi-tenant
    routing is off or the root doesn't exist -- admin endpoints
    then return empty results rather than 5xx.
    """
    if not _per_user_workspaces_enabled():
        return []
    root = _workspaces_root()
    if not root.exists() or not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _read_company_block(yaml_path: pathlib.Path) -> dict:
    """Best-effort read. Missing file or unparseable YAML -> {} so
    a single broken workspace doesn't 5xx the whole admin
    response."""
    if not yaml_path.exists():
        return {}
    try:
        text = yaml_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - bad YAML -> show empty company
        return {}
    if not isinstance(data, dict):
        return {}
    company = data.get("company")
    if not isinstance(company, dict):
        return {}
    return company


def _decode_array(raw: str | None) -> list[str]:
    """Same JSON-array decoder shape as core.discovery, kept local
    so we don't pull discovery into admin's import graph."""
    if not raw:
        return []
    try:
        import json
        v = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _ws_email_from_company(company_yaml: dict) -> Optional[str]:
    v = company_yaml.get("founder_email")
    return v if isinstance(v, str) and v else None


# ---------- endpoints ----------

@router.get(
    "/admin/companies",
    response_model=AdminCompaniesResult,
    summary="List every tenant's company profile (admin only)",
)
def admin_companies(
    _principal: dict = Depends(require_admin),
) -> AdminCompaniesResult:
    out: list[AdminCompany] = []
    skipped: list[SkippedTenant] = []
    for ws_dir in _iter_tenant_workspaces():
        try:
            yaml_path = ws_dir / "config" / "company.yaml"
            company = _read_company_block(yaml_path)
            sectors = company.get("sectors") or []
            if not isinstance(sectors, list):
                sectors = []
            out.append(AdminCompany(
                user_id=ws_dir.name,
                user_email=_ws_email_from_company(company),
                name=company.get("name") or None,
                one_liner=company.get("one_liner") or None,
                stage=company.get("stage") or None,
                sectors=[str(s) for s in sectors],
                business_model=company.get("business_model") or None,
                # Use the company.yaml file mtime as a stable "created"
                # signal for now -- the wizard's PUT bumps mtime each
                # time the operator saves, so this is closer to
                # "last updated" until we add a real created_at column.
                created_at=(
                    datetime.fromtimestamp(
                        yaml_path.stat().st_mtime,
                    ).isoformat()
                    if yaml_path.exists() else None
                ),
            ))
        except Exception as exc:  # noqa: BLE001
            # Review #22: report broken tenants instead of silent
            # skip, so admins can see WHICH workspaces are bad.
            skipped.append(SkippedTenant(
                user_id=ws_dir.name, error=str(exc),
            ))
    return AdminCompaniesResult(
        companies=out, count=len(out), skipped=skipped,
    )


@router.get(
    "/admin/investors",
    response_model=AdminInvestorsResult,
    summary=(
        "List investors across all tenants with global enrichment "
        "joined in (admin only)"
    ),
)
def admin_investors(
    tenant: Optional[str] = Query(
        default=None,
        description="Filter to one tenant's user_id",
    ),
    sector: Optional[str] = Query(
        default=None,
        description=(
            "Case-insensitive substring match against any of the "
            "tenant fund's target_sectors / investors_global sectors"
        ),
    ),
    stage: Optional[str] = Query(
        default=None,
        description=(
            "Case-insensitive substring match against the fund's "
            "stated_stage_focus / investors_global stages"
        ),
    ),
    since: Optional[str] = Query(
        default=None,
        description="ISO date; only partners with last_updated >= since",
    ),
    limit: int = Query(default=500, ge=1, le=5000),
    _principal: dict = Depends(require_admin),
) -> AdminInvestorsResult:
    # Pre-build the global-pool lookup keyed by id for the JOIN.
    global_engine = get_global_engine()
    with global_engine.begin() as conn:
        global_rows = list(conn.execute(select(investors_global)))
    global_by_id = {int(r.id): r for r in global_rows}

    sector_filter = (sector or "").strip().lower() or None
    stage_filter = (stage or "").strip().lower() or None
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None

    out: list[AdminInvestor] = []
    skipped: list[SkippedTenant] = []
    for ws_dir in _iter_tenant_workspaces():
        uid = ws_dir.name
        if tenant and uid != tenant:
            continue
        db_path = ws_dir / "data" / "pipeline.db"
        if not db_path.exists():
            continue
        try:
            engine = get_engine(f"sqlite:///{db_path}")
            with engine.begin() as conn:
                rows = list(conn.execute(
                    select(
                        partners.c.partner_id, partners.c.name,
                        partners.c.last_updated,
                        partners.c.claimed_from_global_id,
                        funds.c.fund_id, funds.c.name, funds.c.domain,
                        funds.c.stated_stage_focus,
                    ).select_from(
                        partners.join(
                            funds,
                            partners.c.fund_id == funds.c.fund_id,
                        )
                    )
                ))
        except Exception as exc:  # noqa: BLE001 - bad tenant DB
            # Review #22: surface skipped tenants in the response.
            skipped.append(SkippedTenant(user_id=uid, error=str(exc)))
            continue

        for r in rows:
            last_updated = r[2]
            if since_dt and isinstance(last_updated, datetime):
                if last_updated < since_dt:
                    continue
            partner_id = r[0]
            partner_name = r[1] or ""
            fund_id = r[4]
            firm = r[5] or ""
            stage_focus = (r[7] or "").lower()
            claim_id = r[3]

            global_row = (
                global_by_id.get(int(claim_id))
                if isinstance(claim_id, int) else None
            )
            sectors = (
                _decode_array(global_row.sectors)
                if global_row is not None else []
            )
            stages = (
                _decode_array(global_row.stages)
                if global_row is not None else []
            )
            geographies = (
                _decode_array(global_row.geographies)
                if global_row is not None else []
            )

            # Sector filter checks BOTH the tenant fund's view +
            # the global pool's tagged sectors.
            if sector_filter:
                searchable = " ".join(
                    str(x).lower()
                    for x in [firm, *sectors, stage_focus]
                )
                if sector_filter not in searchable:
                    continue
            # Stage filter is the same idea against stage fields.
            if stage_filter:
                stage_searchable = " ".join(
                    str(x).lower()
                    for x in [stage_focus, *stages]
                )
                if stage_filter not in stage_searchable:
                    continue

            enriched: dict = {}
            email: Optional[str] = None
            if global_row is not None:
                email = global_row.email or None
                import json as _json
                try:
                    raw = global_row.enriched_fields or "{}"
                    enriched = (
                        _json.loads(raw)
                        if isinstance(raw, str) else {}
                    )
                    if not isinstance(enriched, dict):
                        enriched = {}
                except Exception:  # noqa: BLE001
                    enriched = {}

            out.append(AdminInvestor(
                user_id=uid,
                fund_id=fund_id,
                partner_id=partner_id,
                firm=firm,
                partner=partner_name,
                email=email,
                stages=stages,
                sectors=sectors,
                geographies=geographies,
                claimed_from_global_id=(
                    int(claim_id) if isinstance(claim_id, int) else None
                ),
                global_enriched_fields=enriched,
                last_updated=(
                    last_updated.isoformat()
                    if isinstance(last_updated, datetime) else None
                ),
            ))
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    return AdminInvestorsResult(
        investors=out, count=len(out), skipped=skipped,
    )


@router.get(
    "/admin/tenants",
    response_model=AdminTenantsResult,
    summary="Per-tenant roster + activity counts (admin only)",
)
def admin_tenants(
    _principal: dict = Depends(require_admin),
) -> AdminTenantsResult:
    out: list[AdminTenant] = []
    skipped: list[SkippedTenant] = []
    for ws_dir in _iter_tenant_workspaces():
        uid = ws_dir.name
        yaml_path = ws_dir / "config" / "company.yaml"
        company = _read_company_block(yaml_path)
        email = _ws_email_from_company(company)

        company_count = 1 if company.get("name") else 0
        investor_count = 0
        draft_count = 0
        last_active: Optional[datetime] = None

        db_path = ws_dir / "data" / "pipeline.db"
        if db_path.exists():
            try:
                engine = get_engine(f"sqlite:///{db_path}")
                with engine.begin() as conn:
                    investor_count = (
                        conn.execute(
                            select(func.count()).select_from(partners)
                        ).scalar() or 0
                    )
                    draft_count = (
                        conn.execute(
                            select(func.count()).select_from(email_drafts)
                        ).scalar() or 0
                    )
                    latest_run_completed = conn.execute(
                        select(runs.c.completed_at)
                        .order_by(desc(runs.c.run_id))
                        .limit(1)
                    ).scalar()
                    if isinstance(latest_run_completed, datetime):
                        last_active = latest_run_completed
            except Exception as exc:  # noqa: BLE001 - bad tenant DB
                # Review #22: still emit the tenant row with zero
                # counts, but ALSO surface the error so admins know
                # the numbers are stale / broken.
                skipped.append(SkippedTenant(
                    user_id=uid, error=str(exc),
                ))

        if last_active is None and yaml_path.exists():
            # Fall back to company.yaml mtime so a tenant that
            # signed up but hasn't run the pipeline still shows
            # some activity signal.
            try:
                last_active = datetime.fromtimestamp(
                    yaml_path.stat().st_mtime,
                )
            except OSError:
                last_active = None

        out.append(AdminTenant(
            user_id=uid,
            user_email=email,
            company_count=company_count,
            investor_count=int(investor_count),
            draft_count=int(draft_count),
            last_active_at=(
                last_active.isoformat() if last_active else None
            ),
        ))
    return AdminTenantsResult(
        tenants=out, count=len(out), skipped=skipped,
    )
