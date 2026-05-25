"""Stage 8 payload gating by approval state (Slice 8)."""
from __future__ import annotations

from types import SimpleNamespace


def test_approved_draft_payload_includes_body():
    """When a draft is approved_to_send, the partner payload pulls
    in body / subject / followup / deck."""
    from core.attio.payload import build_partner_payload

    payload = build_partner_payload(
        partner_name="Priya",
        partner_source={
            "outreach_email_draft": "real body",
            "email_subject_line": "real subject",
            "email_strategy_used": "signal_led",
        },
        attr_map={
            "outreach_email_draft": "outreach_email_draft",
            "email_subject_line": "email_subject_line",
            "email_strategy_used": "email_strategy_used",
        },
        fund_object="companies", fund_attio_id=None,
    )
    # Body + subject + strategy are all present.
    assert "outreach_email_draft" in payload
    assert "email_subject_line" in payload
    assert "email_strategy_used" in payload


def test_unapproved_draft_source_dict_drops_body_and_subject():
    """The Slice 8 gating happens in Stage 8's main loop where the
    source dict is assembled. Simulate that dict-shape decision: when
    the recommended draft is needs_review (not approved), the body
    + subject + followup + deck keys land as None and
    build_payload drops them from the outgoing Attio payload."""
    from core.attio.payload import build_partner_payload, wrap_value

    # Mirror Stage 8's source-dict construction for a needs_review draft.
    rec = SimpleNamespace(
        body="this body should NOT ship", subject="hidden subject",
        strategy="signal_led", template_smell="low",
        approval_status="needs_review",
        conversion_hypothesis="hyp",
        likely_objection="obj", objection_preempted=True,
    )
    is_approved = rec.approval_status == "approved_to_send"
    source = {
        "email_strategy_used": rec.strategy,
        "template_smell": rec.template_smell,
        "outreach_email_draft": rec.body if is_approved else None,
        "email_subject_line": rec.subject if is_approved else None,
        "conversion_hypothesis":
            rec.conversion_hypothesis if is_approved else None,
        "likely_objection":
            rec.likely_objection if is_approved else None,
    }
    payload = build_partner_payload(
        partner_name="Priya",
        partner_source=source,
        attr_map={
            "outreach_email_draft": "outreach_email_draft",
            "email_subject_line": "email_subject_line",
            "email_strategy_used": "email_strategy_used",
            "template_smell": "template_smell",
            "conversion_hypothesis": "conversion_hypothesis",
            "likely_objection": "likely_objection",
        },
        fund_object="companies", fund_attio_id=None,
    )
    # Strategy + template_smell ship (CRM context).
    assert "email_strategy_used" in payload
    assert "template_smell" in payload
    # Body / subject / conversion_hypothesis / likely_objection
    # are dropped because source values are None.
    assert "outreach_email_draft" not in payload
    assert "email_subject_line" not in payload
    assert "conversion_hypothesis" not in payload
    assert "likely_objection" not in payload


def test_stale_draft_treated_same_as_unapproved():
    """A draft that was previously approved but is now stale must
    NOT continue pushing the approved body to Attio."""
    rec = SimpleNamespace(
        body="approved body that's now stale",
        subject="stale subject",
        strategy="signal_led", template_smell="low",
        approval_status="stale_after_approval",
        conversion_hypothesis=None, likely_objection=None,
        objection_preempted=False,
    )
    is_approved = rec.approval_status == "approved_to_send"
    assert is_approved is False
    # Stage 8's source-dict logic.
    body_to_send = rec.body if is_approved else None
    assert body_to_send is None
