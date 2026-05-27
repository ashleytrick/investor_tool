"""Batch G: AttioCRMClient.list_pipeline_updates_since against
real Attio v2 query payloads (mocked HTTP).

Pre-batch-G the method returned `[]` unconditionally. Now it
actually calls Attio's records-query endpoint and parses the
deal-record shape into the pipeline-update dict expected by
poll_crm_pipeline_for_workspace.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest


def _attio_response(records: list[dict]) -> MagicMock:
    """Build a MagicMock httpx Response shaped like Attio's
    POST /v2/objects/{slug}/records/query payload."""
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value={"data": records})
    return r


@pytest.fixture
def attio_client():
    from core.crm_polling import AttioCRMClient
    return AttioCRMClient(api_key="test-key")


def _attio_record(
    *,
    stage: str,
    email: str,
    updated_at: str = "2026-05-27T10:00:00Z",
    notes: str | None = None,
) -> dict:
    """Construct one Attio deal record in the v2 shape."""
    values = {
        "stage": [{"option": {"title": stage}}],
        "associated_people": [{
            "target_record": {
                "values": {
                    "email_addresses": [
                        {"email_address": email},
                    ],
                },
            },
        }],
    }
    if notes is not None:
        values["notes"] = [{"value": notes}]
    return {"updated_at": updated_at, "values": values}


# ---------- happy path ----------

def test_list_pipeline_updates_parses_deal_records(attio_client) -> None:
    records = [
        _attio_record(
            stage="meeting-set",
            email="sam@acme.com",
            updated_at="2026-05-27T10:00:00Z",
            notes="intro call booked",
        ),
        _attio_record(
            stage="lead",
            email="dana@beta.com",
            updated_at="2026-05-26T08:00:00Z",
        ),
    ]
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=_attio_response(records))
        )
        out = attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert len(out) == 2
    assert out[0]["partner_email"] == "sam@acme.com"
    assert out[0]["stage"] == "meeting-set"
    assert out[0]["notes"] == "intro call booked"
    assert out[1]["partner_email"] == "dana@beta.com"
    assert out[1]["stage"] == "lead"
    assert out[1]["notes"] is None


def test_list_pipeline_updates_lowercases_email(attio_client) -> None:
    """Email match against local partners is case-insensitive at
    the poller layer (poll_crm_pipeline_for_workspace builds a
    lowercase dict). The poller-side helper now does the lower()."""
    records = [_attio_record(stage="lead", email="MIXED@case.com")]
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=_attio_response(records))
        )
        out = attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert out[0]["partner_email"] == "mixed@case.com"


# ---------- edge cases ----------

def test_list_pipeline_updates_skips_records_with_no_stage(attio_client) -> None:
    """A deal record with the stage attribute missing (operator
    just created it, hasn't classified yet) is silently skipped
    -- can't fire auto-stop without a stage value."""
    records = [
        {"updated_at": "2026-05-27T10:00:00Z", "values": {}},
        _attio_record(stage="lead", email="ok@x.com"),
    ]
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=_attio_response(records))
        )
        out = attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert len(out) == 1
    assert out[0]["partner_email"] == "ok@x.com"


def test_list_pipeline_updates_skips_records_with_no_linked_person(
    attio_client,
) -> None:
    """Without a linked person we can't map back to a local
    partner -- skip rather than emit a useless row."""
    records = [{
        "updated_at": "2026-05-27T10:00:00Z",
        "values": {
            "stage": [{"option": {"title": "lead"}}],
            # no associated_people
        },
    }]
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=_attio_response(records))
        )
        out = attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert out == []


def test_list_pipeline_updates_404_returns_empty_silently(attio_client) -> None:
    """Operator's Attio doesn't have a 'deals' object (or
    renamed it without setting ATTIO_DEALS_OBJECT). We don't
    raise -- the cron just no-ops until they configure."""
    bad = MagicMock()
    bad.status_code = 404
    bad.text = "not found"
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=bad)
        )
        out = attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert out == []


def test_list_pipeline_updates_500_raises_crm_poll_error(attio_client) -> None:
    """5xx from Attio surfaces as CRMPollError so the
    poll_crm_pipeline_for_workspace scatter-gather records the
    failure per-tenant rather than silently dropping the cron."""
    from core.crm_polling import CRMPollError
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "internal server error"
    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(return_value=bad)
        )
        with pytest.raises(CRMPollError):
            attio_client.list_pipeline_updates_since(
                _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
            )


# ---------- env override ----------

def test_list_pipeline_updates_honors_env_overrides(
    attio_client, monkeypatch,
) -> None:
    """Operator with custom-named deal object sets
    ATTIO_DEALS_OBJECT=opportunities; the URL changes accordingly."""
    monkeypatch.setenv("ATTIO_DEALS_OBJECT", "opportunities")
    captured = {}

    def _capture_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        return _attio_response([])

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = (
            MagicMock(side_effect=_capture_post)
        )
        attio_client.list_pipeline_updates_since(
            _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
        )
    assert "opportunities" in captured["url"]
    assert "deals" not in captured["url"]


# ---------- _attio_extract_value helper ----------

class TestAttioExtractValue:
    def test_handles_select_option(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value(
            [{"option": {"title": "lead"}}]
        ) == "lead"

    def test_handles_text_value(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value([{"value": "  foo  "}]) == "foo"

    def test_handles_email_address(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value(
            [{"email_address": "x@y.z"}]
        ) == "x@y.z"

    def test_none_returns_none(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value(None) is None

    def test_non_list_returns_none(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value("not a list") == "not a list"
        assert _attio_extract_value(42) is None

    def test_empty_list_returns_none(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value([]) is None

    def test_unknown_shape_returns_none(self) -> None:
        from core.crm_polling import _attio_extract_value
        assert _attio_extract_value([{"weird": "shape"}]) is None
