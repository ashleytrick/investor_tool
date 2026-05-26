"""Homepage-driven fund-page discovery for Stage 2.

Operators usually know a fund homepage/domain, not the exact team,
portfolio, or thesis URLs. These helpers extract internal homepage links,
score them by likely enrichment value, and hand Stage 2 a small set of
candidate pages to fetch in addition to the fixed fallback paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from selectolax.parser import HTMLParser


PAGE_CATEGORIES: tuple[str, ...] = (
    "team", "portfolio", "thesis", "news", "about",
)

# High-signal tokens for the kinds of pages Stage 2 needs. Weighted so
# people/team pages rise above generic /about pages when both are present.
CATEGORY_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    "team": (
        ("investment team", 12), ("our team", 11), ("team", 10),
        ("people", 10), ("partners", 9), ("partner", 8),
        ("investors", 7), ("leadership", 7), ("who we are", 6),
        ("firm", 4),
    ),
    "portfolio": (
        ("portfolio", 10), ("companies", 9), ("investments", 9),
        ("our companies", 8), ("founders", 4),
    ),
    "thesis": (
        ("thesis", 10), ("approach", 8), ("sectors", 7),
        ("strategy", 6), ("focus", 5), ("what we invest in", 9),
    ),
    "news": (
        ("news", 8), ("blog", 7), ("insights", 7), ("press", 6),
        ("updates", 5), ("resources", 4),
    ),
    "about": (
        ("about us", 9), ("about", 8), ("company", 4),
        ("mission", 4), ("story", 4),
    ),
}

LOW_VALUE_TOKENS: tuple[str, ...] = (
    "login", "signin", "sign-in", "privacy", "terms", "legal",
    "cookie", "careers", "jobs", "contact", "newsletter",
    "subscribe", "apply", "lp-login",
)


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    category: str
    score: int
    anchor_text: str


def _canonical_host(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_site_host(target_host: str, home_host: str) -> bool:
    """Best-effort same-site check without adding a publicsuffix dep.

    Exact host matches are accepted. Subdomains are accepted in either
    direction so links from `fund.com` to `portfolio.fund.com` and from
    `www.fund.com` to `fund.com` stay discoverable. This intentionally does
    not try to collapse unrelated domains like `fund.co` and `fund.com`.
    """
    target = _canonical_host(target_host)
    home = _canonical_host(home_host)
    return (
        target == home
        or target.endswith("." + home)
        or home.endswith("." + target)
    )


def _normalize_url(url: str) -> str:
    clean, _frag = urldefrag(url)
    parsed = urlparse(clean)
    if not parsed.scheme or not parsed.netloc:
        return clean
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    netloc = parsed.netloc.lower()
    return parsed._replace(netloc=netloc, path=path).geturl()


def _is_internal(url: str, homepage_url: str) -> bool:
    target = urlparse(url)
    home = urlparse(homepage_url)
    if target.scheme not in ("http", "https"):
        return False
    return _same_site_host(target.netloc, home.netloc)


def extract_internal_links(homepage_html: str, homepage_url: str) -> list[tuple[str, str]]:
    """Return unique internal (url, anchor_text) pairs from homepage HTML.

    Relative links are resolved against `homepage_url`; fragments are stripped;
    mailto/tel/javascript links and external hosts are ignored. Same-site
    subdomains are kept because fund sites often put portfolio/blog/team pages
    on `portfolio.fund.com`, `blog.fund.com`, or similar hosts.
    """
    tree = HTMLParser(homepage_html or "")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = _normalize_url(urljoin(homepage_url, href))
        if not _is_internal(url, homepage_url):
            continue
        if url in seen:
            continue
        seen.add(url)
        text = node.text(separator=" ", strip=True) or ""
        out.append((url, text))
    return out


def score_fund_link(url: str, anchor_text: str) -> dict[str, int]:
    """Score a link for each Stage 2 enrichment category."""
    parsed = urlparse(url)
    haystack = " ".join([
        parsed.netloc.replace("-", " ").replace(".", " ").lower(),
        parsed.path.replace("-", " ").replace("_", " ").lower(),
        (anchor_text or "").lower(),
    ])
    if any(tok in haystack for tok in LOW_VALUE_TOKENS):
        return {}
    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for token, weight in keywords:
            if token in haystack:
                score += weight
        if score > 0:
            # Shallower pages are more likely to be canonical overview pages
            # than deep articles or individual portfolio-company pages.
            depth = len([p for p in parsed.path.split("/") if p])
            scores[category] = max(1, score - max(0, depth - 2))
    return scores


def discover_fund_pages(
    homepage_url: str,
    homepage_html: str,
    *,
    per_category: int = 3,
    max_pages: int = 10,
) -> list[DiscoveredPage]:
    """Rank likely fund-enrichment pages discovered from a homepage.

    Returns a deduped list ordered by score desc. A URL can match multiple
    categories; its strongest category wins so Stage 2 does not fetch the same
    page repeatedly.
    """
    best_by_url: dict[str, DiscoveredPage] = {}
    category_counts: dict[str, int] = {cat: 0 for cat in PAGE_CATEGORIES}
    scored: list[DiscoveredPage] = []
    for url, text in extract_internal_links(homepage_html, homepage_url):
        for category, score in score_fund_link(url, text).items():
            scored.append(DiscoveredPage(
                url=url, category=category, score=score, anchor_text=text,
            ))
    scored.sort(key=lambda p: (-p.score, p.category, p.url))
    for page in scored:
        if category_counts.get(page.category, 0) >= per_category:
            continue
        current = best_by_url.get(page.url)
        if current is not None and current.score >= page.score:
            continue
        if current is None:
            category_counts[page.category] = category_counts.get(page.category, 0) + 1
        best_by_url[page.url] = page
    ranked = sorted(best_by_url.values(), key=lambda p: (-p.score, p.category, p.url))
    return ranked[:max_pages]
