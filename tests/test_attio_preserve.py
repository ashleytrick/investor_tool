"""Unit tests for core/attio/preserve.py (Refactor item 7)."""
from __future__ import annotations

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.attio.preserve import (
    existing_partner_state,
    strip_preserved_fields,
)


# ----- existing_partner_state -----


def test_existing_partner_state_pulls_option_and_scalars() -> None:
    record = {
        "values": {
            "outreach_status": [{"option": {"title": "sent"}}],
            "manual_score_override": [{"value": True}],
            "manual_recommended_override": [{"value": False}],
        },
    }
    state = existing_partner_state(record)
    assert state["outreach_status"] == "sent"
    assert state["manual_score_override"] is True
    assert state["manual_recommended_override"] is False


def test_existing_partner_state_missing_values_yields_defaults() -> None:
    state = existing_partner_state({})
    assert state["outreach_status"] is None
    assert state["manual_score_override"] is False
    assert state["manual_recommended_override"] is False


def test_existing_partner_state_tolerates_shape_drift() -> None:
    """Attio occasionally returns nested shapes that don't match the
    documented values[].option.title pattern (e.g. raw strings). The
    extractor should swallow and return None rather than KeyError."""
    record = {
        "values": {
            "outreach_status": "definitely_not_a_list_or_option",
        },
    }
    state = existing_partner_state(record)
    assert state["outreach_status"] is None


# ----- strip_preserved_fields: preserve-on-outreach-started -----


def test_strip_preserves_fields_when_outreach_status_triggers() -> None:
    """When existing.outreach_status is in the preserve.statuses set,
    every db_key in preserve.preserved_fields is removed from the
    outgoing payload."""
    payload = {
        "composite_fit_score": 7.5,
        "name": "Dana",  # never preserved
    }
    existing = {"outreach_status": "sent"}
    cfg = {
        "preserve_on_outreach_started": {
            "statuses": ["sent", "replied"],
            "preserved_fields": ["composite_fit_score"],
        },
    }
    attr_map = {"composite_fit_score": "composite_fit_score_v2"}
    # First test: preserved_fields lists db_key; attr_map translates to
    # api slug. The payload uses the api slug.
    payload[attr_map["composite_fit_score"]] = payload.pop(
        "composite_fit_score",
    )
    new_payload, removed = strip_preserved_fields(
        payload, existing, cfg, attr_map,
    )
    assert "composite_fit_score_v2" not in new_payload
    assert "name" in new_payload
    assert removed == ["composite_fit_score_v2"]


def test_strip_no_op_when_status_not_in_preserve_list() -> None:
    payload = {"composite_fit_score": 7.5}
    existing = {"outreach_status": "draft"}
    cfg = {
        "preserve_on_outreach_started": {
            "statuses": ["sent"],
            "preserved_fields": ["composite_fit_score"],
        },
    }
    new_payload, removed = strip_preserved_fields(
        payload, existing, cfg, {},
    )
    assert new_payload == {"composite_fit_score": 7.5}
    assert removed == []


def test_strip_db_key_falls_back_to_itself_when_attr_map_missing() -> None:
    """attr_map is allowed to be empty -- in that case the db_key is
    used directly as the api slug."""
    payload = {"composite_fit_score": 7.5}
    existing = {"outreach_status": "sent"}
    cfg = {
        "preserve_on_outreach_started": {
            "statuses": ["sent"],
            "preserved_fields": ["composite_fit_score"],
        },
    }
    new_payload, removed = strip_preserved_fields(
        payload, existing, cfg, {},
    )
    assert "composite_fit_score" not in new_payload
    assert removed == ["composite_fit_score"]


def test_strip_skips_keys_not_in_payload() -> None:
    """Preserved-fields config may include keys Stage 8 never sends;
    those shouldn't be reported as removed."""
    payload = {"name": "Dana"}
    existing = {"outreach_status": "sent"}
    cfg = {
        "preserve_on_outreach_started": {
            "statuses": ["sent"],
            "preserved_fields": ["composite_fit_score", "spiky_belief_score"],
        },
    }
    new_payload, removed = strip_preserved_fields(payload, existing, cfg, {})
    assert new_payload == {"name": "Dana"}
    assert removed == []


# ----- strip_preserved_fields: manual override protection -----


def test_manual_score_override_strips_score_group() -> None:
    payload = {"composite_fit_score": 7.5, "send_now_priority": 30.0,
               "outreach_status": "ready_to_send"}
    existing = {"manual_score_override": True}
    cfg = {
        "manual_override_protection": {
            "if_manual_score_override_true_preserve": [
                "composite_fit_score", "send_now_priority",
            ],
        },
    }
    new_payload, removed = strip_preserved_fields(payload, existing, cfg, {})
    assert "composite_fit_score" not in new_payload
    assert "send_now_priority" not in new_payload
    assert "outreach_status" in new_payload
    assert set(removed) == {"composite_fit_score", "send_now_priority"}


def test_manual_recommended_override_strips_recommended_group() -> None:
    payload = {"recommended_to_send": True, "outreach_status": "ready_to_send"}
    existing = {"manual_recommended_override": True}
    cfg = {
        "manual_override_protection": {
            "if_manual_recommended_override_true_preserve": [
                "recommended_to_send",
            ],
        },
    }
    new_payload, removed = strip_preserved_fields(payload, existing, cfg, {})
    assert "recommended_to_send" not in new_payload
    assert removed == ["recommended_to_send"]


def test_both_overrides_active_strips_union() -> None:
    payload = {
        "composite_fit_score": 7.5,
        "recommended_to_send": True,
        "outreach_status": "ready_to_send",
    }
    existing = {
        "manual_score_override": True,
        "manual_recommended_override": True,
    }
    cfg = {
        "manual_override_protection": {
            "if_manual_score_override_true_preserve": ["composite_fit_score"],
            "if_manual_recommended_override_true_preserve":
                ["recommended_to_send"],
        },
    }
    new_payload, removed = strip_preserved_fields(payload, existing, cfg, {})
    assert "composite_fit_score" not in new_payload
    assert "recommended_to_send" not in new_payload
    assert set(removed) == {"composite_fit_score", "recommended_to_send"}


def test_payload_returned_is_the_same_object() -> None:
    """Document the in-place mutation contract: the caller often
    writes `payload, removed = strip_preserved_fields(payload, ...)`
    so the return must be the same dict instance."""
    payload = {"x": 1}
    new_payload, _ = strip_preserved_fields(payload, {}, {}, {})
    assert new_payload is payload


def test_empty_attio_cfg_is_a_no_op() -> None:
    """An older workspace whose attio.yaml doesn't have either of the
    two config blocks should pass through unchanged, not raise."""
    payload = {"composite_fit_score": 7.5}
    existing = {"outreach_status": "sent", "manual_score_override": True}
    new_payload, removed = strip_preserved_fields(
        payload, existing, {}, {},
    )
    assert new_payload == {"composite_fit_score": 7.5}
    assert removed == []
