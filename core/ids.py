"""Canonical, deterministic ID generation. Tenant-agnostic pure functions.

  fund_id    = normalized domain
  partner_id = slug(fund_domain + "_" + normalized_partner_name)

slug: lowercase, whitespace -> "_", keep only [a-z0-9._-]. This preserves the
domain's dot and the name-joining underscore, so the IDs stay human-readable
and stable across pipeline stages.
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_KEEP = re.compile(r"[^a-z0-9._-]")


def normalize_name(name: str) -> str:
    """Lowercase, collapse internal whitespace, strip ends."""
    return _WS.sub(" ", (name or "").strip().lower())


def slug(text: str) -> str:
    """Lowercase; whitespace -> underscore; drop chars outside [a-z0-9._-]."""
    s = _WS.sub("_", (text or "").strip().lower())
    return _KEEP.sub("", s)


def normalize_domain(raw: str) -> str:
    """Strip scheme, leading www., path, and case. '' if nothing usable."""
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def fund_id_for(domain: str) -> str:
    """Canonical fund id: the normalized domain."""
    return normalize_domain(domain)


def partner_id_for(fund_domain: str, partner_name: str) -> str:
    """Deterministic partner id per PROJECT_BRIEF."""
    return slug(f"{normalize_domain(fund_domain)}_{normalize_name(partner_name)}")
