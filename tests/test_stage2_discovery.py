from __future__ import annotations

import pytest

from core.http_client import FetchResult
from core.stage2.discovery import (
    discover_fund_pages,
    extract_internal_links,
    score_fund_link,
)
from core.stage2.fetch import gather_live_pages


HOME_HTML = """
<html>
  <body>
    <nav>
      <a href="/people">People</a>
      <a href="/investment-team">Investment Team</a>
      <a href="/companies">Companies</a>
      <a href="/who-we-are">Who We Are</a>
      <a href="/insights">Insights</a>
      <a href="https://external.example/team">External Team</a>
      <a href="mailto:hello@example.com">Email</a>
    </nav>
  </body>
</html>
"""


def test_extract_internal_links_resolves_and_filters() -> None:
    links = extract_internal_links(HOME_HTML, "https://fund.example/")
    urls = {url for url, _text in links}
    assert "https://fund.example/people" in urls
    assert "https://fund.example/investment-team" in urls
    assert "https://fund.example/companies" in urls
    assert "https://external.example/team" not in urls
    assert not any(url.startswith("mailto:") for url in urls)


def test_score_fund_link_prioritizes_team_pages() -> None:
    scores = score_fund_link(
        "https://fund.example/investment-team",
        "Investment Team",
    )
    assert scores["team"] >= 10
    assert "portfolio" not in scores


def test_discover_fund_pages_categorizes_common_fund_links() -> None:
    pages = discover_fund_pages("https://fund.example/", HOME_HTML)
    by_url = {p.url: p for p in pages}
    assert by_url["https://fund.example/people"].category == "team"
    assert by_url["https://fund.example/investment-team"].category == "team"
    assert by_url["https://fund.example/companies"].category == "portfolio"
    assert by_url["https://fund.example/who-we-are"].category in {"team", "about"}
    assert by_url["https://fund.example/insights"].category == "news"


@pytest.mark.asyncio
async def test_gather_live_pages_fetches_discovered_team_page(monkeypatch) -> None:
    responses = {
        "https://fund.example/": FetchResult(
            url="https://fund.example/",
            status=200,
            text=HOME_HTML,
            final_url="https://fund.example/",
        ),
        "https://fund.example/people": FetchResult(
            url="https://fund.example/people",
            status=200,
            text="<html><body>Alex Partner, General Partner</body></html>",
            final_url="https://fund.example/people",
        ),
        "https://fund.example/investment-team": FetchResult(
            url="https://fund.example/investment-team",
            status=200,
            text="<html><body>Investment Team</body></html>",
            final_url="https://fund.example/investment-team",
        ),
        "https://fund.example/companies": FetchResult(
            url="https://fund.example/companies",
            status=200,
            text="<html><body>Portfolio companies</body></html>",
            final_url="https://fund.example/companies",
        ),
        "https://fund.example/who-we-are": FetchResult(
            url="https://fund.example/who-we-are",
            status=200,
            text="<html><body>About the firm</body></html>",
            final_url="https://fund.example/who-we-are",
        ),
        "https://fund.example/insights": FetchResult(
            url="https://fund.example/insights",
            status=200,
            text="<html><body>Recent insights</body></html>",
            final_url="https://fund.example/insights",
        ),
    }
    fetched: list[str] = []

    async def fake_fetch(self, url: str):
        fetched.append(url)
        return responses.get(url, FetchResult(
            url=url, status=404, text="", final_url=url,
        ))

    monkeypatch.setattr("core.http_client.HttpClient.fetch", fake_fetch)

    pages, required_failures, optional_failures = await gather_live_pages({
        "domain": "fund.example",
    })

    assert required_failures == []
    assert "https://fund.example/people" in pages
    assert "https://fund.example/investment-team" in pages
    assert "https://fund.example/companies" in pages
    assert fetched[0] == "https://fund.example/"
    assert "https://fund.example/people" in fetched
    assert not any("no likely team" in reason for _url, reason in optional_failures)
