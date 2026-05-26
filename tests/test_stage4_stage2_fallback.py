from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine

from core.db import funds, metadata, partners, source_snapshots
from core.stage4.fetch import _stage2_partner_sources


def test_stage2_partner_sources_match_partner_name_on_fund_page() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(funds.insert().values(
            fund_id="fund.example",
            name="Fund Example",
            domain="fund.example",
            last_updated=now,
        ))
        conn.execute(partners.insert().values(
            partner_id="fund.example_alex_partner",
            fund_id="fund.example",
            name="Alex Partner",
            title="General Partner",
            last_updated=now,
        ))
        conn.execute(source_snapshots.insert().values(
            source_url="https://fund.example/people",
            final_url="https://fund.example/people",
            fetched_at=now,
            http_status=200,
            content_hash="h1",
            extracted_text="Alex Partner is a General Partner investing in infra.",
            fetched_during_stage="02_enrich_funds",
        ))
        conn.execute(source_snapshots.insert().values(
            source_url="https://fund.example/portfolio",
            final_url="https://fund.example/portfolio",
            fetched_at=now,
            http_status=200,
            content_hash="h2",
            extracted_text="Portfolio company founder Jordan Founder raised seed.",
            fetched_during_stage="02_enrich_funds",
        ))

    found = _stage2_partner_sources(engine, {"fund.example_alex_partner"})

    assert list(found) == ["fund.example_alex_partner"]
    sources = found["fund.example_alex_partner"]
    assert len(sources) == 1
    assert sources[0]["source_url"] == "https://fund.example/people"
    assert sources[0]["source_type"] == "fund_profile"


def test_stage2_partner_sources_ignore_other_funds() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(funds.insert(), [
            {
                "fund_id": "fund.example",
                "name": "Fund Example",
                "domain": "fund.example",
                "last_updated": now,
            },
            {
                "fund_id": "other.example",
                "name": "Other Example",
                "domain": "other.example",
                "last_updated": now,
            },
        ])
        conn.execute(partners.insert().values(
            partner_id="fund.example_alex_partner",
            fund_id="fund.example",
            name="Alex Partner",
            title="General Partner",
            last_updated=now,
        ))
        conn.execute(source_snapshots.insert().values(
            source_url="https://other.example/team",
            final_url="https://other.example/team",
            fetched_at=now,
            http_status=200,
            content_hash="h1",
            extracted_text="Alex Partner is mentioned as a co-investor here.",
            fetched_during_stage="02_enrich_funds",
        ))

    found = _stage2_partner_sources(engine, {"fund.example_alex_partner"})

    assert found == {}
