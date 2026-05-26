"""Pydantic schemas for the deck-first onboarding extraction.

The endpoint `POST /config/company/extract-from-deck` parses a
PDF/PPTX deck, asks the LLM to populate a draft `CompanyProfile`,
and returns the result. Critically, this is a SETUP ASSISTANT only:
the response is NEVER persisted to `company.yaml`. The frontend
shows extracted fields with evidence/confidence, the operator
reviews, and only then does `PUT /config/company` write the
canonical config.

Schemas here cover:

- `ExtractedField` -- a single per-field row with evidence + a
  confidence score the frontend uses to flag "needs review".
- `DeckLLMOutput` -- the JSON shape the LLM produces. Wraps the
  flat CompanyProfile fields with per-field evidence; the web layer
  collapses this into a final `ExtractionResult`.
- `ExtractionResult` -- the HTTP response. Returned to the frontend
  with `profile` (a draft CompanyProfile) plus the audit trail
  (extracted_fields, missing_required_fields, needs_review_fields,
  warnings, text_preview).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# Confidence floor for "this is reliable; just confirm it." Below
# the threshold, the frontend nudges the operator to review.
NEEDS_REVIEW_THRESHOLD = 0.7

# Required-for-setup field names. The endpoint refuses to call this
# "ready" until every one is non-empty -- the frontend lists missing
# ones so the operator knows what to fill in. Keep in sync with the
# CompanyProfile required-for-setup list in the spec.
REQUIRED_FIELDS: tuple[str, ...] = (
    "name",
    "one_liner",
    "founder_name",
    "founder_email",
    "stage",
    "problem",
    "solution",
    "traction",
    "target_sectors",
    "scheduling_link",
)


class ExtractedField(BaseModel):
    """One per-field extraction. The frontend renders these inline
    next to the form input so the operator sees WHERE the value
    came from before they decide to accept it."""
    field: str = Field(..., min_length=1)
    # Value rendered as JSON for the frontend -- strings, ints,
    # lists are all valid CompanyProfile values; using `str | int |
    # list[str] | None` keeps the response readable without forcing
    # the frontend to deserialize a generic JSON blob.
    value: str | int | list[str] | None = None
    # 0.0 to 1.0 -- the LLM's stated certainty. The schema doesn't
    # clamp because Pydantic-level validation isn't worth a rejected
    # response for a 1.01; the renderer treats >=0.7 as "ok",
    # 0.4-0.7 as "review", <0.4 as "uncertain".
    confidence: float = 0.0
    # Short excerpt or paraphrase from the deck text. ALWAYS surfaced
    # in the response so the operator can audit -- empty evidence
    # means the LLM inferred the value without a clear citation
    # (rare; the prompt heavily discourages it).
    evidence: str = ""
    # "slide 4", "page 2", or "" when the LLM couldn't localize the
    # claim. Free-text on purpose -- some decks number slides, some
    # don't, and a "slide N" string is more readable than an int.
    source: str = ""


class DeckLLMOutput(BaseModel):
    """JSON contract the LLM emits. The shape is FLAT (one
    ExtractedField per CompanyProfile attribute) because asking the
    LLM to recurse into nested CompanyProfile shape adds noise
    without buying anything -- the web layer reshapes into the
    response."""
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    # Free-text problems the LLM noticed: image-heavy deck, sparse
    # text, conflicting claims, etc. Surfaced to the operator so
    # they know whether to trust the auto-fill or start manually.
    warnings: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """HTTP response from `POST /config/company/extract-from-deck`.

    Doesn't itself trigger a write to `company.yaml` -- the frontend
    populates the form with `profile`, surfaces the per-field
    evidence, then calls `PUT /config/company` once the operator
    has reviewed.
    """
    # The draft CompanyProfile to pre-fill the form. Stored as
    # `dict` here (not the strongly-typed CompanyProfile) because
    # this module sits below web.api -- defining the type here
    # would either move CompanyProfile out or introduce a cycle.
    # The web layer constructs / validates a CompanyProfile from
    # this dict before returning.
    profile: dict = Field(default_factory=dict)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    needs_review_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_filename: str = ""
    # First N chars of the raw extracted deck text. Used by the
    # frontend's "we read this from your deck" preview and by tests
    # to assert extraction actually ran (vs. a silent empty path).
    text_preview: str = ""
