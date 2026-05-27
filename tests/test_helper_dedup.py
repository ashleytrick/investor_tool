"""Audit-review batch F: dedup of helpers.

Covers:
  - core.discovery.slug_unclaimed_domain is the canonical
    firm -> {slug}.unclaimed builder. core.crm_polling and
    web.routers.investors delegate to it.
  - web.deps.parse_future_iso_naive_utc is the canonical
    future-ISO parser; coach + investors snooze handlers
    delegate to it with their respective field_name args.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from fastapi import HTTPException


# ---------- slug_unclaimed_domain ----------

def test_canonical_slug_basic() -> None:
    from core.discovery import slug_unclaimed_domain
    assert slug_unclaimed_domain("Sequoia Capital") == "sequoia-capital.unclaimed"


def test_canonical_slug_keeps_alnum_replaces_other_with_dash() -> None:
    """Canonical rule: each non-alnum becomes a dash (no collapse).
    Stable behavior the deterministic fund_id_for() depends on."""
    from core.discovery import slug_unclaimed_domain
    # "A.B. Capital, LLC" -> dot, space, comma each become dashes;
    # trailing dashes trimmed.
    assert slug_unclaimed_domain("A.B. Capital, LLC") == "a-b--capital--llc.unclaimed"


def test_canonical_slug_empty_returns_empty() -> None:
    from core.discovery import slug_unclaimed_domain
    assert slug_unclaimed_domain("") == ""
    assert slug_unclaimed_domain("   ") == ""


def test_canonical_slug_back_compat_alias() -> None:
    """Existing callers in core.discovery imported _slug_domain by
    name; the alias preserves them."""
    from core.discovery import _slug_domain, slug_unclaimed_domain
    assert _slug_domain is slug_unclaimed_domain


def test_crm_polling_delegates_to_canonical() -> None:
    """core.crm_polling.poll_crm_investors_for_workspace's local
    `_slug_unclaimed` name now resolves to the canonical helper."""
    from core.crm_polling import _slug_unclaimed
    from core.discovery import slug_unclaimed_domain
    assert _slug_unclaimed is slug_unclaimed_domain


def test_investors_router_delegates_to_canonical() -> None:
    """web.routers.investors's _slug_unclaimed_domain is the
    canonical helper; capture endpoint uses it for pseudo-domain."""
    from web.routers.investors import _slug_unclaimed_domain
    from core.discovery import slug_unclaimed_domain
    assert _slug_unclaimed_domain is slug_unclaimed_domain


# ---------- parse_future_iso_naive_utc ----------

def test_future_iso_parses_valid_iso() -> None:
    from web.deps import parse_future_iso_naive_utc
    future = (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    ).isoformat()
    out = parse_future_iso_naive_utc(future, field_name="x")
    assert out.tzinfo is None  # contract: naive UTC


def test_future_iso_rejects_past() -> None:
    from web.deps import parse_future_iso_naive_utc
    past = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)
    ).isoformat()
    with pytest.raises(HTTPException) as exc_info:
        parse_future_iso_naive_utc(past, field_name="snoozed_until")
    assert exc_info.value.status_code == 422
    assert "snoozed_until" in exc_info.value.detail


def test_future_iso_rejects_garbage() -> None:
    from web.deps import parse_future_iso_naive_utc
    with pytest.raises(HTTPException) as exc_info:
        parse_future_iso_naive_utc("not-a-date", field_name="until")
    assert exc_info.value.status_code == 422
    assert "until" in exc_info.value.detail


def test_future_iso_field_name_appears_in_error() -> None:
    """The point of the field_name kwarg: 422 messages should
    name the field that the operator actually sent so frontend
    UX is clear."""
    from web.deps import parse_future_iso_naive_utc
    with pytest.raises(HTTPException) as exc:
        parse_future_iso_naive_utc("garbage", field_name="defer_until")
    assert "defer_until" in exc.value.detail
