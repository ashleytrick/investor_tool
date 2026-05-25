"""Unit tests for core/attribution/status.py + review_queue.py (Slice 6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.attribution.review_queue import (
    KIND_AMBIGUOUS_ATTRIBUTION,
    list_pending,
    queue_review,
    resolve,
)
from core.attribution.status import (
    ALL_MATCHED_BY,
    ALL_MATCH_STATUSES,
    MATCHED_BY_EXACT,
    MATCHED_BY_FUND_NAME,
    STATUS_AMBIGUOUS,
    STATUS_CONFIRMED,
    STATUS_LIKELY,
    STATUS_REJECTED,
    STATUS_UNMATCHED,
    STRONG_LIKELY_MIN,
    classify_from_candidates,
    counts_toward_scoring,
)
from core.db import get_engine


# ----- counts_toward_scoring -----


def test_confirmed_always_counts() -> None:
    assert counts_toward_scoring(match_status=STATUS_CONFIRMED, match_confidence=0.5)
    assert counts_toward_scoring(match_status=STATUS_CONFIRMED, match_confidence=None)


def test_strong_likely_counts() -> None:
    assert counts_toward_scoring(
        match_status=STATUS_LIKELY, match_confidence=STRONG_LIKELY_MIN,
    )
    assert counts_toward_scoring(
        match_status=STATUS_LIKELY, match_confidence=0.99,
    )


def test_weak_likely_does_not_count() -> None:
    assert not counts_toward_scoring(
        match_status=STATUS_LIKELY,
        match_confidence=STRONG_LIKELY_MIN - 0.01,
    )
    assert not counts_toward_scoring(
        match_status=STATUS_LIKELY, match_confidence=None,
    )


def test_ambiguous_rejected_unmatched_never_count() -> None:
    for s in (STATUS_AMBIGUOUS, STATUS_REJECTED, STATUS_UNMATCHED):
        assert not counts_toward_scoring(
            match_status=s, match_confidence=1.0,
        )


def test_unknown_status_does_not_count() -> None:
    assert not counts_toward_scoring(
        match_status="weird_status", match_confidence=1.0,
    )
    assert not counts_toward_scoring(
        match_status=None, match_confidence=1.0,
    )


# ----- classify_from_candidates -----


def test_classify_no_chosen_is_unmatched() -> None:
    s, m = classify_from_candidates(
        chosen_fund_id=None, candidates=[],
        ambiguity_delta=0.05, fuzzy_threshold=0.85,
    )
    assert s == STATUS_UNMATCHED
    assert m is None


def test_classify_exact_match_is_confirmed() -> None:
    cands = [{"id": "f1", "name": "Northbeam Capital", "score": 1.0}]
    s, m = classify_from_candidates(
        chosen_fund_id="f1", candidates=cands,
        ambiguity_delta=0.05, fuzzy_threshold=0.85,
    )
    assert s == STATUS_CONFIRMED
    assert m == MATCHED_BY_EXACT


def test_classify_high_fuzzy_with_clear_winner_is_likely() -> None:
    cands = [
        {"id": "f1", "name": "Northbeam", "score": 0.90},
        {"id": "f2", "name": "Other", "score": 0.40},
    ]
    s, m = classify_from_candidates(
        chosen_fund_id="f1", candidates=cands,
        ambiguity_delta=0.05, fuzzy_threshold=0.85,
    )
    assert s == STATUS_LIKELY
    assert m == MATCHED_BY_FUND_NAME


def test_classify_close_second_candidate_is_ambiguous() -> None:
    """Top 0.88, second 0.86, delta 0.05 -> diff 0.02 < delta -> ambig."""
    cands = [
        {"id": "f1", "name": "Northbeam", "score": 0.88},
        {"id": "f2", "name": "Northbeam Ventures", "score": 0.86},
    ]
    s, m = classify_from_candidates(
        chosen_fund_id="f1", candidates=cands,
        ambiguity_delta=0.05, fuzzy_threshold=0.85,
    )
    assert s == STATUS_AMBIGUOUS
    assert m == MATCHED_BY_FUND_NAME


def test_classify_low_score_below_threshold_is_unmatched() -> None:
    cands = [{"id": "f1", "name": "Maybe?", "score": 0.40}]
    s, m = classify_from_candidates(
        chosen_fund_id="f1", candidates=cands,
        ambiguity_delta=0.05, fuzzy_threshold=0.85,
    )
    assert s == STATUS_UNMATCHED
    assert m is None


def test_status_and_matched_by_constant_sets_self_consistent() -> None:
    """Guard: all named STATUS_ constants are in ALL_MATCH_STATUSES."""
    for s in (
        STATUS_CONFIRMED, STATUS_LIKELY, STATUS_AMBIGUOUS,
        STATUS_REJECTED, STATUS_UNMATCHED,
    ):
        assert s in ALL_MATCH_STATUSES
    for m in (MATCHED_BY_EXACT, MATCHED_BY_FUND_NAME):
        assert m in ALL_MATCHED_BY


# ----- review_queue -----


@pytest.fixture
def engine(tmp_path: Path):
    return get_engine(f"sqlite:///{tmp_path / 'test.db'}")


def test_queue_review_inserts_one_pending_row(engine) -> None:
    review_id = queue_review(
        engine, kind=KIND_AMBIGUOUS_ATTRIBUTION,
        target_id="42",
        context={"candidates": [{"id": "f1", "score": 0.88}]},
    )
    assert review_id > 0
    pending = list_pending(engine)
    assert len(pending) == 1
    assert pending[0].kind == KIND_AMBIGUOUS_ATTRIBUTION
    assert pending[0].target_id == "42"


def test_queue_review_is_idempotent_on_same_pending_target(engine) -> None:
    a = queue_review(engine, kind="ambiguous_attribution", target_id="42")
    b = queue_review(engine, kind="ambiguous_attribution", target_id="42")
    assert a == b
    assert len(list_pending(engine)) == 1


def test_queue_review_distinguishes_kinds(engine) -> None:
    queue_review(engine, kind="ambiguous_attribution", target_id="42")
    queue_review(engine, kind="pending_approval", target_id="42")
    assert len(list_pending(engine)) == 2
    assert len(list_pending(engine, kind="ambiguous_attribution")) == 1


def test_resolve_marks_resolved(engine) -> None:
    rid = queue_review(engine, kind="ambiguous_attribution", target_id="42")
    resolve(engine, review_id=rid, resolved_by="ashley", notes="picked f1")
    # No longer pending.
    assert list_pending(engine) == []


def test_resolve_idempotent_on_already_resolved(engine) -> None:
    rid = queue_review(engine, kind="ambiguous_attribution", target_id="42")
    resolve(engine, review_id=rid, resolved_by="ashley")
    # Second call doesn't raise.
    resolve(engine, review_id=rid, resolved_by="ashley")


def test_resolve_rejects_unknown_status(engine) -> None:
    rid = queue_review(engine, kind="ambiguous_attribution", target_id="42")
    with pytest.raises(ValueError):
        resolve(engine, review_id=rid, resolved_by="x", status="weird")
