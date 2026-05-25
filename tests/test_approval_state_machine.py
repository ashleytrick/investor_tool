"""Unit tests for core/approval/state_machine.py (Slice 1)."""
from __future__ import annotations

import pytest

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.approval.state_machine import (
    ALL_STATES,
    APPROVED_STATES,
    REVIEWABLE_STATES,
    STATE_APPROVED_TO_SEND,
    STATE_NEEDS_REVIEW,
    STATE_REJECTED,
    STATE_SENT,
    STATE_STALE_AFTER_APPROVAL,
    InvalidApprovalTransition,
    allowed_next_states,
    assert_can_transition,
    is_approved,
    is_reviewable,
    is_terminal,
)


# ----- state-set sanity -----


def test_all_states_includes_every_named_state() -> None:
    """Guard against drift: if someone adds a new STATE_* constant
    they must add it to ALL_STATES."""
    named = {
        STATE_NEEDS_REVIEW, STATE_APPROVED_TO_SEND, STATE_REJECTED,
        STATE_STALE_AFTER_APPROVAL, STATE_SENT,
    }
    assert ALL_STATES == frozenset(named)


def test_approved_states_is_exactly_one_state() -> None:
    """The 'send queue' gate must filter on a single canonical state.
    A wider definition (e.g. 'approved with warning') needs explicit
    design before the readers fan it out."""
    assert APPROVED_STATES == frozenset({STATE_APPROVED_TO_SEND})


def test_reviewable_states_covers_needs_review_and_stale() -> None:
    assert REVIEWABLE_STATES == frozenset({
        STATE_NEEDS_REVIEW, STATE_STALE_AFTER_APPROVAL,
    })


# ----- transition table -----


def test_initial_seed_is_only_into_needs_review() -> None:
    """Stage 7's draft insert must seed needs_review and only
    needs_review; any other initial state would skip the review."""
    assert_can_transition(None, STATE_NEEDS_REVIEW, source="system")
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(None, STATE_APPROVED_TO_SEND)


def test_human_approves_from_needs_review() -> None:
    src = assert_can_transition(
        STATE_NEEDS_REVIEW, STATE_APPROVED_TO_SEND, source="human",
    )
    assert src == "human"


def test_system_cannot_approve_drafts() -> None:
    """The whole point: only a human can move into approved_to_send.
    A system-source edge into approved must raise."""
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(
            STATE_NEEDS_REVIEW, STATE_APPROVED_TO_SEND, source="system",
        )


def test_human_can_reject_from_needs_review_or_approved() -> None:
    assert_can_transition(
        STATE_NEEDS_REVIEW, STATE_REJECTED, source="human",
    )
    assert_can_transition(
        STATE_APPROVED_TO_SEND, STATE_REJECTED, source="human",
    )


def test_system_can_stale_an_approval() -> None:
    """The invalidation rules (regeneration, email change,
    do_not_contact set, relationship change, score change) move an
    approved draft to stale_after_approval. System source only."""
    assert_can_transition(
        STATE_APPROVED_TO_SEND, STATE_STALE_AFTER_APPROVAL, source="system",
    )


def test_stale_to_approved_requires_human() -> None:
    """An operator re-approving a stale draft is a human action.
    The system can never auto-re-approve."""
    assert_can_transition(
        STATE_STALE_AFTER_APPROVAL, STATE_APPROVED_TO_SEND, source="human",
    )
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(
            STATE_STALE_AFTER_APPROVAL, STATE_APPROVED_TO_SEND,
            source="system",
        )


def test_sent_is_terminal() -> None:
    """Once a draft is sent, no further transitions are allowed.
    Reply / passed / meeting are outcomes (separate table)."""
    assert is_terminal(STATE_SENT)
    for to_state in ALL_STATES:
        with pytest.raises(InvalidApprovalTransition):
            assert_can_transition(STATE_SENT, to_state)


def test_unknown_target_state_rejected() -> None:
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(STATE_NEEDS_REVIEW, "not_a_real_state")


def test_disallowed_edge_rejected() -> None:
    """rejected -> approved_to_send is NOT allowed; the operator
    must un-reject (rejected -> needs_review) first."""
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(
            STATE_REJECTED, STATE_APPROVED_TO_SEND, source="human",
        )


# ----- helper predicates -----


def test_is_approved_only_true_for_approved_to_send() -> None:
    assert is_approved(STATE_APPROVED_TO_SEND) is True
    for s in ALL_STATES - APPROVED_STATES:
        assert is_approved(s) is False


def test_is_reviewable_for_needs_review_and_stale() -> None:
    assert is_reviewable(STATE_NEEDS_REVIEW) is True
    assert is_reviewable(STATE_STALE_AFTER_APPROVAL) is True
    assert is_reviewable(STATE_APPROVED_TO_SEND) is False
    assert is_reviewable(STATE_REJECTED) is False
    assert is_reviewable(STATE_SENT) is False


def test_allowed_next_states_for_needs_review() -> None:
    nxt = allowed_next_states(STATE_NEEDS_REVIEW)
    assert nxt == {
        STATE_APPROVED_TO_SEND,
        STATE_REJECTED,
        STATE_STALE_AFTER_APPROVAL,
    }


def test_allowed_next_states_for_initial_seed() -> None:
    assert allowed_next_states(None) == {STATE_NEEDS_REVIEW}


def test_allowed_next_states_for_terminal_sent() -> None:
    assert allowed_next_states(STATE_SENT) == set()


def test_source_mismatch_human_on_system_edge_rejected() -> None:
    """Stale-after-approval is system-only. A CLI calling with
    source='human' on that edge is a bug -- catch it."""
    with pytest.raises(InvalidApprovalTransition):
        assert_can_transition(
            STATE_APPROVED_TO_SEND, STATE_STALE_AFTER_APPROVAL,
            source="human",
        )


def test_source_none_skips_source_check() -> None:
    """When source isn't supplied, only the edge existence is
    validated. Lets pure dry-run callers (UI 'is this allowed?'
    queries) check transitions without committing to an actor."""
    # Both should succeed without raising.
    assert_can_transition(STATE_NEEDS_REVIEW, STATE_APPROVED_TO_SEND)
    assert_can_transition(
        STATE_APPROVED_TO_SEND, STATE_STALE_AFTER_APPROVAL,
    )
