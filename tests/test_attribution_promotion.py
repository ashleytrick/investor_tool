"""Unit tests for core/attribution/promotion.py (Slice 12)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.attribution.promotion import (
    PromotionError,
    bulk_reattribute_deals,
    promote_provisional_fund,
    promote_provisional_partner,
)
from core.attribution.status import (
    MATCHED_BY_MANUAL,
    STATUS_CONFIRMED,
    STATUS_LIKELY,
    STATUS_UNMATCHED,
)
from core.db import deal_attributions, funds, get_engine, partners


@pytest.fixture
def engine(tmp_path: Path):
    return get_engine(f"sqlite:///{tmp_path / 'test.db'}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _insert_fund(engine, fund_id: str, *, name: str, domain: str,
                 is_provisional: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id=fund_id, name=name, domain=domain,
            is_active=True, is_provisional=is_provisional,
            last_updated=_now(),
        ))


def _insert_partner(engine, partner_id: str, *, fund_id: str, name: str,
                    is_provisional: bool = False) -> None:
    with engine.begin() as conn:
        conn.execute(partners.insert().values(
            partner_id=partner_id, fund_id=fund_id, name=name,
            employment_status="uncertain",
            is_provisional=is_provisional, last_updated=_now(),
        ))


def _insert_deal(engine, *, lead_fund_id: str | None,
                 attributed_partner_id: str | None = None,
                 source_url: str, company: str = "Acme",
                 match_status: str = STATUS_UNMATCHED) -> int:
    with engine.begin() as conn:
        result = conn.execute(deal_attributions.insert().values(
            company=company, round_type="Series A",
            announcement_date=date.today(),
            lead_fund_id=lead_fund_id,
            attributed_partner_id=attributed_partner_id,
            source_url=source_url,
            captured_at=_now(),
            match_status=match_status,
        ))
        return int(result.inserted_primary_key[0])


# ----- promote_provisional_fund -----


def test_promote_provisional_fund_clears_flag(engine) -> None:
    _insert_fund(engine, "f.prov", name="Acme", domain="acme.provisional",
                 is_provisional=True)
    result = promote_provisional_fund(engine, fund_id="f.prov")
    assert result.cleared_provisional is True
    with engine.begin() as conn:
        row = conn.execute(funds.select().where(funds.c.fund_id == "f.prov")).first()
    assert row.is_provisional is False


def test_promote_provisional_fund_renames_and_sets_domain(engine) -> None:
    _insert_fund(engine, "f.prov", name="Acme", domain="acme.provisional",
                 is_provisional=True)
    promote_provisional_fund(
        engine, fund_id="f.prov",
        new_name="Acme Capital", new_domain="acme.com",
    )
    with engine.begin() as conn:
        row = conn.execute(funds.select().where(funds.c.fund_id == "f.prov")).first()
    assert row.name == "Acme Capital"
    assert row.domain == "acme.com"
    assert row.is_provisional is False


def test_promote_provisional_fund_refuses_missing(engine) -> None:
    with pytest.raises(PromotionError, match="not found"):
        promote_provisional_fund(engine, fund_id="nope")


def test_promote_provisional_fund_refuses_non_provisional(engine) -> None:
    _insert_fund(engine, "f.real", name="Acme", domain="acme.com",
                 is_provisional=False)
    with pytest.raises(PromotionError, match="already non-provisional"):
        promote_provisional_fund(engine, fund_id="f.real")


# ----- promote_provisional_partner -----


def test_promote_provisional_partner_clears_flag(engine) -> None:
    _insert_fund(engine, "f.real", name="Acme", domain="acme.com")
    _insert_partner(engine, "p.prov", fund_id="f.real", name="Jane Doe",
                    is_provisional=True)
    result = promote_provisional_partner(engine, partner_id="p.prov")
    assert result.cleared_provisional is True
    with engine.begin() as conn:
        row = conn.execute(
            partners.select().where(partners.c.partner_id == "p.prov")
        ).first()
    assert row.is_provisional is False


def test_promote_provisional_partner_updates_metadata(engine) -> None:
    _insert_fund(engine, "f.real", name="Acme", domain="acme.com")
    _insert_partner(engine, "p.prov", fund_id="f.real", name="Jane Doe",
                    is_provisional=True)
    promote_provisional_partner(
        engine, partner_id="p.prov",
        new_name="Jane Q. Doe", new_title="Partner",
        new_linkedin="https://linkedin.com/in/janedoe",
    )
    with engine.begin() as conn:
        row = conn.execute(
            partners.select().where(partners.c.partner_id == "p.prov")
        ).first()
    assert row.name == "Jane Q. Doe"
    assert row.title == "Partner"
    assert row.linkedin_url == "https://linkedin.com/in/janedoe"


def test_promote_provisional_partner_refuses_missing(engine) -> None:
    with pytest.raises(PromotionError, match="not found"):
        promote_provisional_partner(engine, partner_id="nope")


def test_promote_provisional_partner_refuses_non_provisional(engine) -> None:
    _insert_fund(engine, "f.real", name="Acme", domain="acme.com")
    _insert_partner(engine, "p.real", fund_id="f.real", name="Jane",
                    is_provisional=False)
    with pytest.raises(PromotionError, match="already non-provisional"):
        promote_provisional_partner(engine, partner_id="p.real")


# ----- bulk_reattribute_deals -----


def test_bulk_reattribute_moves_all_deals(engine) -> None:
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_fund(engine, "f.other", name="Other", domain="other.com")
    _insert_deal(engine, lead_fund_id="f.src", source_url="https://a/1")
    _insert_deal(engine, lead_fund_id="f.src", source_url="https://a/2")
    _insert_deal(engine, lead_fund_id="f.other", source_url="https://a/3")

    result = bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst", actor="ashley",
    )
    assert result.deals_moved == 2
    assert result.dry_run is False

    with engine.begin() as conn:
        for row in conn.execute(deal_attributions.select()):
            if row.source_url in ("https://a/1", "https://a/2"):
                assert row.lead_fund_id == "f.dst"
                assert row.match_status == STATUS_CONFIRMED
                assert row.matched_by == MATCHED_BY_MANUAL
                assert row.reviewed_by == "ashley"
                assert row.reviewed_at is not None
            else:
                assert row.lead_fund_id == "f.other"  # untouched


def test_bulk_reattribute_dry_run_writes_nothing(engine) -> None:
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_deal(engine, lead_fund_id="f.src", source_url="https://a/1",
                 match_status=STATUS_UNMATCHED)

    result = bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst",
        actor="ashley", dry_run=True,
    )
    assert result.deals_moved == 1
    assert result.dry_run is True

    with engine.begin() as conn:
        row = conn.execute(deal_attributions.select()).first()
    assert row.lead_fund_id == "f.src"
    assert row.match_status == STATUS_UNMATCHED


def test_bulk_reattribute_remaps_partner_by_name(engine) -> None:
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_partner(engine, "p.src.jane", fund_id="f.src", name="Jane Doe")
    _insert_partner(engine, "p.dst.jane", fund_id="f.dst", name="Jane Doe")
    _insert_deal(
        engine, lead_fund_id="f.src", attributed_partner_id="p.src.jane",
        source_url="https://a/1",
    )

    result = bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst",
        actor="ashley", also_remap_partners=True,
    )
    assert result.deals_moved == 1
    assert result.partners_remapped == 1
    assert result.partners_orphaned == []

    with engine.begin() as conn:
        row = conn.execute(deal_attributions.select()).first()
    assert row.lead_fund_id == "f.dst"
    assert row.attributed_partner_id == "p.dst.jane"


def test_bulk_reattribute_orphans_partner_when_no_match(engine) -> None:
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_partner(engine, "p.src.jane", fund_id="f.src", name="Jane Doe")
    # No matching partner in f.dst.
    _insert_deal(
        engine, lead_fund_id="f.src", attributed_partner_id="p.src.jane",
        source_url="https://a/1",
    )

    result = bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst",
        actor="ashley", also_remap_partners=True,
    )
    assert result.deals_moved == 1
    assert result.partners_remapped == 0
    assert result.partners_orphaned == ["p.src.jane"]

    with engine.begin() as conn:
        row = conn.execute(deal_attributions.select()).first()
    assert row.lead_fund_id == "f.dst"
    assert row.attributed_partner_id is None


def test_bulk_reattribute_without_remap_keeps_partner(engine) -> None:
    """When also_remap_partners is False, the partner id is preserved
    on the moved row even though it points at the OLD fund's partner.
    Operator decides whether to clean up afterward."""
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_partner(engine, "p.src.jane", fund_id="f.src", name="Jane Doe")
    _insert_deal(
        engine, lead_fund_id="f.src", attributed_partner_id="p.src.jane",
        source_url="https://a/1",
    )

    result = bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst", actor="ashley",
    )
    assert result.partners_remapped == 0
    assert result.partners_orphaned == []

    with engine.begin() as conn:
        row = conn.execute(deal_attributions.select()).first()
    assert row.lead_fund_id == "f.dst"
    assert row.attributed_partner_id == "p.src.jane"


def test_bulk_reattribute_refuses_self(engine) -> None:
    _insert_fund(engine, "f.x", name="X", domain="x.com")
    with pytest.raises(PromotionError, match="same"):
        bulk_reattribute_deals(
            engine, from_fund_id="f.x", to_fund_id="f.x", actor="ashley",
        )


def test_bulk_reattribute_refuses_missing_source(engine) -> None:
    _insert_fund(engine, "f.dst", name="Dst", domain="dst.com")
    with pytest.raises(PromotionError, match="from_fund_id.*not found"):
        bulk_reattribute_deals(
            engine, from_fund_id="f.gone", to_fund_id="f.dst",
            actor="ashley",
        )


def test_bulk_reattribute_refuses_missing_dest(engine) -> None:
    _insert_fund(engine, "f.src", name="Src", domain="src.com")
    with pytest.raises(PromotionError, match="to_fund_id.*not found"):
        bulk_reattribute_deals(
            engine, from_fund_id="f.src", to_fund_id="f.gone",
            actor="ashley",
        )


def test_promote_provisional_merge_into_moves_deals_and_deactivates_source(engine) -> None:
    """Loose end from Slice 12 / shipped in Slice 13:
    `promote_provisional --merge-into <fund_id>` should re-attribute
    every deal from the provisional fund and deactivate the source.

    Exercises the same underlying primitives (bulk_reattribute_deals +
    funds.update) the CLI calls, so the unit test stays at the core
    layer."""
    from datetime import date as _date
    _insert_fund(engine, "f.prov", name="Acme (provisional)",
                 domain="acme.provisional", is_provisional=True)
    _insert_fund(engine, "f.real", name="Acme Capital", domain="acme.com")
    _insert_deal(engine, lead_fund_id="f.prov", source_url="https://a/1")
    _insert_deal(engine, lead_fund_id="f.prov", source_url="https://a/2")

    # The CLI runs bulk_reattribute_deals then update funds. Replay
    # that pair here so any regression in either step is caught.
    result = bulk_reattribute_deals(
        engine, from_fund_id="f.prov", to_fund_id="f.real",
        actor="ashley",
    )
    assert result.deals_moved == 2
    from sqlalchemy import update as _update
    with engine.begin() as conn:
        conn.execute(
            _update(funds).where(funds.c.fund_id == "f.prov")
            .values(is_active=False, is_provisional=False,
                    last_updated=_now())
        )

    with engine.begin() as conn:
        src = conn.execute(
            funds.select().where(funds.c.fund_id == "f.prov")
        ).first()
        deal_funds = {
            r.lead_fund_id for r in conn.execute(deal_attributions.select())
        }
    assert src.is_active is False
    assert src.is_provisional is False
    assert deal_funds == {"f.real"}


def test_bulk_reattribute_likely_becomes_confirmed(engine) -> None:
    """A `likely` (weak) row that the operator manually moves should
    flip to `confirmed` so Stage 6 starts counting it."""
    _insert_fund(engine, "f.src", name="Old", domain="old.com")
    _insert_fund(engine, "f.dst", name="New", domain="new.com")
    _insert_deal(
        engine, lead_fund_id="f.src", source_url="https://a/1",
        match_status=STATUS_LIKELY,
    )
    bulk_reattribute_deals(
        engine, from_fund_id="f.src", to_fund_id="f.dst", actor="ashley",
    )
    with engine.begin() as conn:
        row = conn.execute(deal_attributions.select()).first()
    assert row.match_status == STATUS_CONFIRMED
