"""Meeting-prep builders (Build Session 12).

Two LLM-driven artifacts gated on a partner having earned a reply
(outreach_status IN ('replied', 'meeting_booked')):

  - objection_map: 5-7 partner-specific objections, each tied to a
    verified signal_id (or labeled sector_norm).
  - framing_brief: how to tell THIS company's story to THIS partner
    -- lead, amplify, address unprompted, do not lead with, the
    closing question.

Both are persisted to `meeting_prep_artifacts` keyed on a hash of the
verified signal set, so re-running prep_brief.py against unchanged
evidence costs zero LLM calls.

This module deliberately does NOT touch cold-outreach drafting --
the email pipeline (Stage 7) has its own discipline and budget. Use
this for the meeting itself.
"""
