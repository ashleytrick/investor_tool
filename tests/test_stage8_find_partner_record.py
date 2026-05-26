"""Regression for the post-PR-29 finding that
scripts/08_sync_to_attio.find_partner_record matched the email step
without scoping to the partner's fund / company. An operator who
reused a personal Gmail across two funds would get cross-linked to
whichever Attio Person was returned first.

Stubs an AttioClient so we can pin the query_records result per call
and assert what find_partner_record decided.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_stage8():
    """Stage 8 lives at scripts/08_sync_to_attio.py and isn't a
    package member; load it by path so we can call its module-level
    helpers directly."""
    spec = importlib.util.spec_from_file_location(
        "stage8_test_module",
        str(REPO_ROOT / "scripts" / "08_sync_to_attio.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stage8_test_module"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    """Drives the find_partner_record cascade by returning a canned
    list per `query_records` call."""

    def __init__(self, by_filter: list[tuple[dict, list[dict]]]):
        # list of (expected_filter, results_to_return); pop in order.
        self._script = list(by_filter)
        self.calls: list[dict] = []

    def query_records(self, person_object, filter_, *, limit=1):
        self.calls.append({"filter": filter_, "limit": limit})
        # Match strategy-agnostically: every call returns whatever the
        # next scripted entry says, regardless of the filter shape --
        # the test author is responsible for ordering.
        if not self._script:
            return []
        _, results = self._script.pop(0)
        return list(results)


def test_email_match_at_different_company_is_rejected():
    """A Person with the same email but linked to a DIFFERENT
    company_record_id must NOT be returned. find_partner_record
    should fall through to linkedin / name+company instead."""
    stage8 = _import_stage8()
    client = _FakeClient(by_filter=[
        # Email step: returns a person, but their company is "co_B".
        (
            {"email_addresses": "shared@op.com"},
            [{
                "id": {"record_id": "person_B"},
                "values": {"company": [{"target_record_id": "co_B"}]},
            }],
        ),
        # LinkedIn step: returns nothing.
        ({"linkedin_url": "https://linkedin.com/in/x"}, []),
        # Name+company step: returns a person linked to OUR company.
        (
            {"name": "Alice", "company": {"target_record_id": "co_A"}},
            [{"id": {"record_id": "person_A_correct"}}],
        ),
    ])
    result = stage8.find_partner_record(
        client, "people",
        email="shared@op.com",
        linkedin_url="https://linkedin.com/in/x",
        name="Alice",
        company_record_id="co_A",
    )
    assert result is not None, "should have fallen through to name+company"
    rid = result.get("id", {}).get("record_id")
    assert rid == "person_A_correct", (
        f"find_partner_record returned wrong person: {result}"
    )


def test_email_match_at_same_company_is_accepted():
    """When the email-matched Person's company matches our
    company_record_id, accept the hit -- no need to fall through."""
    stage8 = _import_stage8()
    client = _FakeClient(by_filter=[
        (
            {"email_addresses": "alice@op.com"},
            [{
                "id": {"record_id": "person_A"},
                "values": {"company": [{"target_record_id": "co_A"}]},
            }],
        ),
    ])
    result = stage8.find_partner_record(
        client, "people",
        email="alice@op.com",
        linkedin_url=None,
        name="Alice",
        company_record_id="co_A",
    )
    assert result is not None
    assert result["id"]["record_id"] == "person_A"


def test_email_match_with_no_company_link_is_accepted():
    """Attio Person not yet linked to any company -> we can safely
    claim it for our partner (no cross-fund risk)."""
    stage8 = _import_stage8()
    client = _FakeClient(by_filter=[
        (
            {"email_addresses": "alice@op.com"},
            [{"id": {"record_id": "person_unlinked"}, "values": {}}],
        ),
    ])
    result = stage8.find_partner_record(
        client, "people",
        email="alice@op.com",
        linkedin_url=None,
        name="Alice",
        company_record_id="co_A",
    )
    assert result is not None
    assert result["id"]["record_id"] == "person_unlinked"


def test_email_match_without_company_context_keeps_legacy_behavior():
    """When the caller has no company_record_id, accept the first hit
    (the historical behavior). Only the company-aware path is the
    fix; callers that don't pass company_record_id (rare) get the
    pre-fix shape."""
    stage8 = _import_stage8()
    client = _FakeClient(by_filter=[
        (
            {"email_addresses": "alice@op.com"},
            [{
                "id": {"record_id": "person_X"},
                "values": {"company": [{"target_record_id": "co_X"}]},
            }],
        ),
    ])
    result = stage8.find_partner_record(
        client, "people",
        email="alice@op.com",
        linkedin_url=None,
        name=None,
        company_record_id=None,
    )
    assert result is not None
    assert result["id"]["record_id"] == "person_X"
