"""FR-5: LLM output schema for follow-up draft generation.

A follow-up is generated for touch_number 2..max_touches when a
sequence's `next_touch_due_at` elapses. The shape is intentionally
slimmer than `email_generation.EmailOutput`:

  - No subject (follow-ups thread into the original conversation;
    Gmail Drafts honors `In-Reply-To` for that, and an empty
    subject collapses the thread).
  - No deck_request_response (that's an initial-outreach concept).
  - No batch QA fields (per-touch generation isn't batched the
    same way Stage 7 is).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class FollowUpOutput(BaseModel):
    """Validated output for prompts/generate_followup.txt."""
    # Always empty in practice (follow-ups thread); kept on the
    # schema so future channel variants (e.g. LinkedIn DM with a
    # synthetic subject) can populate it without a schema change.
    subject: str = Field(default="")
    body: str = Field(
        min_length=20,
        description="The follow-up message body, 3-4 sentences.",
    )
    rationale: str = Field(
        min_length=10, max_length=400,
        description=(
            "One-sentence justification for why this touch + angle "
            "should land. Persisted on follow_up_drafts.why_now."
        ),
    )
    # null for most angles. specific_ask MAY emit a one-sentence
    # objection-preempt addition (matches Stage 7's
    # preemption_line concept but optional).
    preempted_objection: str | None = None
