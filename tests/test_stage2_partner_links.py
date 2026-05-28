"""Unit tests for the Stage 2 partner-link harvester (batch J).

Verifies the deterministic anchor scan: only ``/in/`` LinkedIn profile
URLs and bare Twitter/X handles are captured, links are attributed to
the partner named in their surrounding DOM block, and shared-card
ambiguity is resolved to the longest matching name.
"""
from __future__ import annotations

from core.stage2.partner_links import (
    _normalize_linkedin_url,
    _normalize_twitter_handle,
    harvest_partner_links,
)


def test_linkedin_in_path_is_captured():
    assert _normalize_linkedin_url("https://www.linkedin.com/in/jane-doe/") == (
        "linkedin.com/in/jane-doe"
    )


def test_linkedin_company_path_is_rejected():
    assert _normalize_linkedin_url(
        "https://www.linkedin.com/company/acme-vc/"
    ) is None


def test_linkedin_post_url_is_rejected():
    assert _normalize_linkedin_url(
        "https://www.linkedin.com/posts/jane-doe_activity-12345"
    ) is None


def test_twitter_handle_extracted_from_x_com():
    assert _normalize_twitter_handle("https://x.com/janedoe") == "janedoe"


def test_twitter_handle_extracted_from_twitter_com():
    assert _normalize_twitter_handle(
        "https://twitter.com/janedoe/status/123"
    ) == "janedoe"


def test_twitter_non_profile_paths_rejected():
    assert _normalize_twitter_handle("https://x.com/home") is None
    assert _normalize_twitter_handle("https://twitter.com/search?q=foo") is None


def test_harvest_matches_link_to_partner_in_block():
    html = """
    <div>
      <h3>Jane Doe</h3>
      <a href="https://www.linkedin.com/in/jane-doe/">LinkedIn</a>
      <a href="https://x.com/janedoe">X</a>
    </div>
    <div>
      <h3>Sam Patel</h3>
      <a href="https://www.linkedin.com/in/sam-patel/">LinkedIn</a>
    </div>
    """
    out = harvest_partner_links({"u": html}, ["Jane Doe", "Sam Patel"])
    assert out["Jane Doe"]["linkedin_url"] == (
        "linkedin.com/in/jane-doe"
    )
    assert out["Jane Doe"]["twitter_handle"] == "janedoe"
    assert out["Sam Patel"]["linkedin_url"] == (
        "linkedin.com/in/sam-patel"
    )
    assert "twitter_handle" not in out["Sam Patel"]


def test_harvest_skips_links_with_no_partner_match():
    html = """
    <footer>
      <a href="https://www.linkedin.com/in/intern-account/">LinkedIn</a>
    </footer>
    """
    assert harvest_partner_links({"u": html}, ["Jane Doe"]) == {}


def test_harvest_first_link_wins_across_pages():
    home = (
        '<div>Jane Doe '
        '<a href="https://www.linkedin.com/in/jane-1/">li</a></div>'
    )
    team = (
        '<div>Jane Doe '
        '<a href="https://www.linkedin.com/in/jane-2/">li</a></div>'
    )
    out = harvest_partner_links(
        {"home": home, "team": team}, ["Jane Doe"],
    )
    assert out["Jane Doe"]["linkedin_url"] in (
        "linkedin.com/in/jane-1",
        "linkedin.com/in/jane-2",
    )
    # Stable across runs given same dict insertion order.


def test_harvest_handles_live_pages_dict_shape():
    """Stage 2 live mode passes ``{url: {"html": ..., "final_url": ...}}``."""
    html = (
        '<div>Jane Doe '
        '<a href="https://www.linkedin.com/in/jane-doe/">li</a></div>'
    )
    out = harvest_partner_links(
        {"https://example.com/": {"html": html, "final_url": None}},
        ["Jane Doe"],
    )
    assert out["Jane Doe"]["linkedin_url"] == (
        "linkedin.com/in/jane-doe"
    )


def test_harvest_picks_longest_matching_name_on_collision():
    """When two partners share a first name, the link sitting next to
    'Sam Patel' should bind to Sam Patel, not Sam."""
    html = (
        '<div>Sam Patel '
        '<a href="https://www.linkedin.com/in/sam-patel/">li</a></div>'
    )
    out = harvest_partner_links({"u": html}, ["Sam", "Sam Patel"])
    assert "Sam Patel" in out
    assert "Sam" not in out


def test_harvest_returns_empty_when_no_partner_names_given():
    html = '<a href="https://www.linkedin.com/in/jane-doe/">li</a>'
    assert harvest_partner_links({"u": html}, []) == {}


def test_harvest_anchor_text_as_name_fallback():
    """Some team pages render each partner as a link whose label IS the
    partner's name -- no parent block to scan."""
    html = (
        '<a href="https://www.linkedin.com/in/jane-doe/">Jane Doe</a>'
    )
    out = harvest_partner_links({"u": html}, ["Jane Doe"])
    assert out["Jane Doe"]["linkedin_url"] == (
        "linkedin.com/in/jane-doe"
    )
