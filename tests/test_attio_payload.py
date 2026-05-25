"""Unit tests for core/attio/payload.py (Refactor item 7 / 15)."""
from __future__ import annotations

from datetime import date, datetime

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.attio.payload import (
    SELECT_SLUGS,
    build_fund_payload,
    build_partner_payload,
    build_payload,
    wrap_value,
)


# ----- wrap_value -----


def test_wrap_value_none_returns_none() -> None:
    """None / empty-string drop out so the payload omits the field
    rather than sending a NULL clear to Attio."""
    assert wrap_value(None, "anything") is None
    assert wrap_value("", "anything") is None


def test_wrap_value_select_slug_uses_option_title() -> None:
    out = wrap_value("sent", "outreach_status")
    assert out == [{"option": {"title": "sent"}}]


def test_wrap_value_bool_lands_as_value() -> None:
    assert wrap_value(True, "manual_score_override") == [{"value": True}]
    assert wrap_value(False, "manual_score_override") == [{"value": False}]


def test_wrap_value_numeric_lands_as_value() -> None:
    assert wrap_value(7.5, "composite_fit_score") == [{"value": 7.5}]
    assert wrap_value(42, "send_now_priority") == [{"value": 42}]


def test_wrap_value_date_isoformatted() -> None:
    out = wrap_value(date(2026, 5, 24), "scored_at")
    assert out == [{"value": "2026-05-24"}]


def test_wrap_value_datetime_isoformatted() -> None:
    out = wrap_value(datetime(2026, 5, 24, 12, 0), "scored_at")
    assert out == [{"value": "2026-05-24T12:00:00"}]


def test_wrap_value_string_falls_through() -> None:
    assert wrap_value("hello", "name") == [{"value": "hello"}]


def test_wrap_value_select_slug_coerces_non_strings() -> None:
    """Select slugs should always wrap str() of the value so an int
    or enum that landed in a select column still produces a valid
    option title."""
    out = wrap_value(3, "outreach_status")
    assert out == [{"option": {"title": "3"}}]


def test_select_slugs_is_frozen() -> None:
    """Defensive: SELECT_SLUGS is shared across modules; making it
    a frozenset prevents accidental mutation."""
    assert isinstance(SELECT_SLUGS, frozenset)


# ----- build_payload -----


def test_build_payload_basic_attr_map() -> None:
    out = build_payload(
        {"composite_fit_score": "composite", "name": "name"},
        {"composite_fit_score": 7.5, "name": "Dana", "ignored": "x"},
    )
    assert out == {
        "composite": [{"value": 7.5}],
        "name": [{"value": "Dana"}],
    }


def test_build_payload_drops_none_values() -> None:
    """A field whose source value is None must be DROPPED, not sent
    as a NULL clear -- Attio would interpret NULL as 'wipe this'."""
    out = build_payload(
        {"composite_fit_score": "composite", "name": "name"},
        {"composite_fit_score": None, "name": "Dana"},
    )
    assert "composite" not in out
    assert "name" in out


def test_build_payload_drops_empty_strings_too() -> None:
    out = build_payload(
        {"name": "name", "title": "title"},
        {"name": "Dana", "title": ""},
    )
    assert "title" not in out


def test_build_payload_select_slug_handled_via_attr_slug() -> None:
    """The select-shape decision is keyed on the API SLUG (not the DB
    key) -- so a renamed db_key that maps to "outreach_status" still
    gets option-wrapped."""
    out = build_payload(
        {"status_local": "outreach_status"},
        {"status_local": "sent"},
    )
    assert out == {"outreach_status": [{"option": {"title": "sent"}}]}


def test_build_payload_empty_attr_map_returns_empty() -> None:
    assert build_payload({}, {"x": 1}) == {}


# ----- build_fund_payload -----


def test_fund_payload_includes_name_and_domain() -> None:
    out = build_fund_payload(
        fund_name="Northbeam",
        fund_domain="northbeam.example",
        fund_source={},
        attr_map={},
    )
    assert out["name"] == [{"value": "Northbeam"}]
    assert out["domains"] == [{"domain": "northbeam.example"}]


def test_fund_payload_omits_domains_when_missing() -> None:
    """None / empty domain should NOT send domains=[]; Attio would
    interpret that as clearing the domain list."""
    out = build_fund_payload(
        fund_name="Anon Fund", fund_domain=None,
        fund_source={}, attr_map={},
    )
    assert "domains" not in out


def test_fund_payload_omits_domains_when_empty_string() -> None:
    out = build_fund_payload(
        fund_name="Anon Fund", fund_domain="",
        fund_source={}, attr_map={},
    )
    assert "domains" not in out


def test_fund_payload_merges_custom_attrs() -> None:
    out = build_fund_payload(
        fund_name="Northbeam", fund_domain="northbeam.example",
        fund_source={"stated_thesis": "infra at seed", "missing_field": None},
        attr_map={
            "stated_thesis": "thesis",
            "missing_field": "missing",
        },
    )
    assert out["thesis"] == [{"value": "infra at seed"}]
    # None source value -> custom field DROPPED.
    assert "missing" not in out


def test_fund_payload_custom_attr_can_override_base() -> None:
    """If the operator's attr_map happens to include 'name', the custom
    payload's value wins per the dict-merge order. This is documented
    here so a future regression doesn't silently swap base/custom
    precedence."""
    out = build_fund_payload(
        fund_name="Default Name", fund_domain="x.example",
        fund_source={"local_name": "Custom Name"},
        attr_map={"local_name": "name"},
    )
    assert out["name"] == [{"value": "Custom Name"}]


# ----- build_partner_payload -----


def test_partner_payload_includes_name() -> None:
    out = build_partner_payload(
        partner_name="Dana Cole",
        partner_source={},
        attr_map={},
        fund_object="companies",
        fund_attio_id=None,
    )
    assert out["name"] == [{"value": "Dana Cole"}]
    # No fund link if no fund_attio_id.
    assert "company" not in out


def test_partner_payload_attaches_company_ref_when_id_known() -> None:
    out = build_partner_payload(
        partner_name="Dana", partner_source={}, attr_map={},
        fund_object="companies",
        fund_attio_id="company_record_42",
    )
    assert out["company"] == [{
        "target_object": "companies",
        "target_record_id": "company_record_42",
    }]


def test_partner_payload_merges_email_and_select_attrs() -> None:
    out = build_partner_payload(
        partner_name="Dana", partner_source={
            "partner_email": "dana@x.example",
            "outreach_status": "ready_to_send",
            "send_now_priority": 30.0,
            "manual_score_override": False,  # bool=False MUST still appear
        },
        attr_map={
            "partner_email": "email_addresses",
            "outreach_status": "outreach_status",
            "send_now_priority": "send_now",
            "manual_score_override": "manual_score_override",
        },
        fund_object="companies",
        fund_attio_id=None,
    )
    assert out["email_addresses"] == [{"value": "dana@x.example"}]
    assert out["outreach_status"] == [{"option": {"title": "ready_to_send"}}]
    assert out["send_now"] == [{"value": 30.0}]
    # bool False is NOT empty-string-equivalent; it should be wrapped.
    assert out["manual_score_override"] == [{"value": False}]


def test_partner_payload_can_omit_custom_when_attr_map_empty() -> None:
    out = build_partner_payload(
        partner_name="Dana", partner_source={"anything": "value"},
        attr_map={},
        fund_object="companies", fund_attio_id=None,
    )
    # Only base name; no fund link; no custom attrs.
    assert out == {"name": [{"value": "Dana"}]}
