"""Harvest LinkedIn + Twitter URLs for partners from Stage 2 team-page HTML.

Stage 4 previously relied on operators hand-curating
``data/raw/partner_content_urls.csv`` to know each partner's blog /
LinkedIn / podcast. That meant every new fund required manual data
entry before Stage 4 could mine signals.

This module closes the loop for the most-common case -- a fund team
page lists each partner alongside their LinkedIn (and sometimes
Twitter/X) link. We scan the HTML deterministically (no LLM call),
match each social link to the partner whose name appears in the same
block, and return ``{partner_name: {linkedin_url, twitter_handle}}``.
The Stage 2 main loop then upserts these onto the ``partners`` row so
downstream stages (CRM sync, Stage 4 fetch fallback, frontend display)
have the URLs without operator CSV work.

Deterministic, not LLM-driven: we want no chance of hallucinated URLs.
A link is only attributed to a partner if their name literally appears
in the same DOM neighbourhood.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser, Node

from core.ids import normalize_name


_LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com"}
_TWITTER_HOSTS = {
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "mobile.twitter.com",
}
# Twitter/X paths that are not user profiles -- skip them so we don't
# attribute /home or /search to a partner.
_TWITTER_NON_PROFILE_SEGMENTS = {
    "home", "search", "explore", "i", "intent", "share", "tos",
    "privacy", "about", "login", "signup", "messages", "settings",
    "compose", "notifications",
}
_TWITTER_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def _normalize_linkedin_url(href: str) -> str | None:
    """Return a canonical LinkedIn profile URL or None.

    Only ``/in/{slug}`` and ``/pub/{...}`` paths count; company pages
    (``/company/...``) and post URLs are not partner profiles.

    Output shape matches ``web.routers.investors._normalize_linkedin_url``
    so that FR-2 captured partners and Stage 2-harvested partners
    dedupe on the same column via an exact SQL equality.
    """
    parsed = urlparse(href.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in _LINKEDIN_HOSTS:
        return None
    path = parsed.path.rstrip("/")
    if not path:
        return None
    segs = [s for s in path.split("/") if s]
    if not segs or segs[0] not in ("in", "pub"):
        return None
    return f"linkedin.com{path}".lower()


def _normalize_twitter_handle(href: str) -> str | None:
    """Return a bare Twitter handle (no @, no URL) or None."""
    parsed = urlparse(href.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in _TWITTER_HOSTS:
        return None
    path = parsed.path.strip("/")
    if not path:
        return None
    first = path.split("/", 1)[0]
    if first.lower() in _TWITTER_NON_PROFILE_SEGMENTS:
        return None
    if not _TWITTER_HANDLE_RE.match(first):
        return None
    return first


def _matches_in_text(
    text: str, partner_names: list[str],
) -> list[tuple[int, str]]:
    norm_text = normalize_name(text)
    if not norm_text:
        return []
    hits: list[tuple[int, str]] = []
    for name in partner_names:
        norm = normalize_name(name)
        if not norm or norm not in norm_text:
            continue
        hits.append((len(norm), name))
    return hits


def _find_matching_partner_by_ancestor(
    node: Node, partner_names: list[str], *, max_depth: int = 5,
) -> str | None:
    """Walk up from ``node`` looking for the nearest ancestor whose text
    names exactly one partner.

    Stopping at the FIRST single-match ancestor keeps us inside the
    partner's own card -- walking further up reaches a wrapper that
    contains every partner on the page, which would be ambiguous.

    When multiple partner names match at the same level (e.g. one
    partner's first name is a prefix of another's), the longest match
    wins so 'Sam Patel' beats 'Sam' inside a card that names Sam Patel.
    """
    current: Node | None = node.parent
    for _ in range(max_depth):
        if current is None:
            return None
        text = current.text(separator=" ", strip=True) or ""
        hits = _matches_in_text(text, partner_names)
        if hits:
            unique = {name for _, name in hits}
            if len(unique) == 1:
                return next(iter(unique))
            # Multiple distinct partners share this ancestor -- it's
            # the page wrapper. Bail; we'd be guessing.
            longest_at_level = max(hits, key=lambda h: h[0])
            other_at_level = [
                n for ln, n in hits
                if ln < longest_at_level[0]
                and normalize_name(n)
                not in normalize_name(longest_at_level[1])
            ]
            if not other_at_level:
                return longest_at_level[1]
            return None
        current = current.parent
    return None


def harvest_partner_links(
    pages: dict,
    partner_names: list[str],
) -> dict[str, dict[str, str]]:
    """Return ``{partner_name: {"linkedin_url": ..., "twitter_handle": ...}}``.

    ``pages`` is the same shape Stage 2 already passes around:
    ``{url: html_str}`` (fixture mode) or ``{url: {"html": ..., ...}}``
    (live mode).

    Partners with no detected links are omitted from the result so the
    caller can iterate and SET only the populated rows.
    """
    if not partner_names:
        return {}

    out: dict[str, dict[str, str]] = {}
    for entry in pages.values():
        html = entry.get("html") if isinstance(entry, dict) else entry
        if not html:
            continue
        tree = HTMLParser(html)
        for anchor in tree.css("a[href]"):
            href = (anchor.attributes.get("href") or "").strip()
            if not href:
                continue
            linkedin = _normalize_linkedin_url(href)
            twitter = _normalize_twitter_handle(href)
            if not linkedin and not twitter:
                continue
            # Search the ancestor block first (covers cards), then fall
            # back to the anchor's own text (covers anchors whose label
            # is the partner name itself).
            matched = _find_matching_partner_by_ancestor(
                anchor, partner_names,
            )
            if matched is None:
                anchor_text = anchor.text(separator=" ", strip=True) or ""
                hits = _matches_in_text(anchor_text, partner_names)
                if hits:
                    matched = max(hits, key=lambda h: h[0])[1]
            if matched is None:
                continue
            slot = out.setdefault(matched, {})
            # First link wins for each kind so we don't flip-flop between
            # multiple pages that may list the same partner.
            if linkedin and "linkedin_url" not in slot:
                slot["linkedin_url"] = linkedin
            if twitter and "twitter_handle" not in slot:
                slot["twitter_handle"] = twitter
    return out
