"""Unit tests for core/partner_evidence.py (Refactor item 7 / 12)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.partner_evidence import (
    build_reachability_payload,
    format_content_block,
    partner_reachability_values,
    signal_insert_values,
    signal_update_values,
)


_NOW = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def _signal(**over) -> SimpleNamespace:
    base = dict(
        source_type="podcast",
        source_url="https://a.example/ep1",
        quoted_text="we lean into infra plays at seed",
        quote_date=date(2026, 3, 15),
        axis_relevance=["axis_a", "axis_b"],
        signal_direction="positive",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ----- format_content_block -----


def test_format_content_block_one_source() -> None:
    out = format_content_block([{
        "source_url": "https://a.example/p1",
        "source_type": "blog",
        "quote_date": "2026-03-01",
        "text": "raw body",
    }])
    assert out == (
        "--- https://a.example/p1 (blog, 2026-03-01) ---\nraw body"
    )


def test_format_content_block_joins_with_blank_lines() -> None:
    out = format_content_block([
        {"source_url": "https://a", "source_type": "blog",
         "quote_date": "x", "text": "A"},
        {"source_url": "https://b", "source_type": "podcast",
         "quote_date": "y", "text": "B"},
    ])
    # Blank line between sources so the LLM sees them as separate chunks.
    assert "\n\nA" not in out  # A's header isn't preceded by blank
    assert "A\n\n---" in out


def test_format_content_block_missing_quote_date_uses_question_mark() -> None:
    out = format_content_block([{
        "source_url": "https://a", "source_type": "blog", "text": "body",
    }])
    assert "(blog, ?)" in out


def test_format_content_block_empty_list() -> None:
    assert format_content_block([]) == ""


# ----- signal_update_values -----


def test_dedup_update_refreshes_metadata_fields() -> None:
    out = signal_update_values(
        existing_snapshot_id=42,
        new_signal=_signal(),
        new_snapshot_id=42,
    )
    # Metadata fields land verbatim.
    assert out["source_type"] == "podcast"
    assert out["quote_date"] == date(2026, 3, 15)
    assert out["signal_direction"] == "positive"
    # axis_relevance is JSON-serialized for the column.
    assert json.loads(out["axis_relevance"]) == ["axis_a", "axis_b"]


def test_dedup_update_does_not_touch_verified_or_quality() -> None:
    """Stage 5 owns verified + signal_quality_score; this update must
    not include them or it would silently unverify a previously-
    verified signal on a routine Stage 4 re-run."""
    out = signal_update_values(
        existing_snapshot_id=42,
        new_signal=_signal(),
        new_snapshot_id=42,
    )
    assert "verified" not in out
    assert "signal_quality_score" not in out


def test_dedup_update_backfills_snapshot_when_missing() -> None:
    """Existing row had snapshot_id=NULL (older entry from before
    snapshots were captured) and the new run has one -> backfill."""
    out = signal_update_values(
        existing_snapshot_id=None,
        new_signal=_signal(),
        new_snapshot_id=99,
    )
    assert out["snapshot_id"] == 99


def test_dedup_update_preserves_existing_snapshot_id() -> None:
    """When existing row already has a snapshot_id, don't churn it."""
    out = signal_update_values(
        existing_snapshot_id=42,
        new_signal=_signal(),
        new_snapshot_id=99,
    )
    assert "snapshot_id" not in out


def test_dedup_update_no_snapshot_anywhere() -> None:
    """Both sides None -> still no key (avoid writing NULL over NULL
    just to set last_updated, which would churn the row needlessly)."""
    out = signal_update_values(
        existing_snapshot_id=None,
        new_signal=_signal(),
        new_snapshot_id=None,
    )
    assert "snapshot_id" not in out


# ----- signal_insert_values -----


def test_signal_insert_values_shape() -> None:
    out = signal_insert_values(
        partner_id="p1",
        signal=_signal(),
        snapshot_id=42,
        captured_at=_NOW,
    )
    assert out["partner_id"] == "p1"
    assert out["snapshot_id"] == 42
    assert out["source_url"] == "https://a.example/ep1"
    assert out["quoted_text"] == "we lean into infra plays at seed"
    # New rows default to unverified -- Stage 5's gauntlet flips it.
    assert out["verified"] is False
    assert out["captured_at"] == _NOW
    assert json.loads(out["axis_relevance"]) == ["axis_a", "axis_b"]


def test_signal_insert_accepts_none_snapshot_id() -> None:
    """Stage 4 may not have a snapshot (e.g. transient fetch failure
    before this signal was extracted from a stale cache); the row
    still needs to land so Stage 5 sees the quote to verify."""
    out = signal_insert_values(
        partner_id="p1", signal=_signal(),
        snapshot_id=None, captured_at=_NOW,
    )
    assert out["snapshot_id"] is None


# ----- build_reachability_payload -----


def test_reachability_payload_shape() -> None:
    output = SimpleNamespace(
        cold_reachability_reasoning="recent posts; active on LinkedIn",
        reachability_signals=[
            SimpleNamespace(
                evidence="3 posts in last 60 days",
                source_url="https://linkedin.com/in/foo",
                direction="positive",
            ),
            SimpleNamespace(
                evidence="podcast appearance",
                source_url="https://podcast.example/ep",
                direction="positive",
            ),
        ],
    )
    payload = build_reachability_payload(output)
    assert payload["reasoning"] == "recent posts; active on LinkedIn"
    assert len(payload["signals"]) == 2
    assert payload["signals"][0]["evidence"] == "3 posts in last 60 days"


def test_reachability_payload_empty_signals_ok() -> None:
    output = SimpleNamespace(
        cold_reachability_reasoning="nothing recent",
        reachability_signals=[],
    )
    payload = build_reachability_payload(output)
    assert payload["signals"] == []


def test_reachability_payload_serializes_url_to_string() -> None:
    """The source_url may be a Pydantic HttpUrl that doesn't json-
    serialize directly; explicit str() avoids the TypeError when the
    payload lands in the DB via json.dumps()."""
    # Use a non-string sentinel that has a __str__ to confirm the
    # conversion happens inside build_reachability_payload.
    class _Url:
        def __str__(self) -> str:
            return "https://forced.example"
    output = SimpleNamespace(
        cold_reachability_reasoning="",
        reachability_signals=[SimpleNamespace(
            evidence="x", source_url=_Url(), direction="positive",
        )],
    )
    payload = build_reachability_payload(output)
    # The function does str(url) when shaping the dict; the result is
    # JSON-serializable.
    assert payload["signals"][0]["source_url"] == "https://forced.example"
    json.dumps(payload)  # must not raise


# ----- partner_reachability_values -----


def test_partner_reachability_values_serializes_payload() -> None:
    out = partner_reachability_values(
        score=7.5, payload={"reasoning": "x", "signals": []}, now=_NOW,
    )
    assert out["cold_reachability_partial_score"] == 7.5
    # payload column carries JSON string.
    assert json.loads(out["cold_reachability_partial_evidence"]) == {
        "reasoning": "x", "signals": [],
    }
    assert out["last_updated"] == _NOW


def test_partner_reachability_values_none_score() -> None:
    out = partner_reachability_values(
        score=None, payload={"reasoning": "", "signals": []}, now=_NOW,
    )
    assert out["cold_reachability_partial_score"] is None
