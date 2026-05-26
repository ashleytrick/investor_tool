"""Schema for the post-reply Investor Dossier (Build Session 14).

Replaces the narrower objection_map + framing_brief user-facing
artifacts with a single rich dossier modeled on the Christo dossier:

  CONFIDENTIAL -- INVESTOR DOSSIER
  Header (partner / fund / role / location / themes)
  Profile Summary           -- 2-4 paragraphs + meeting-role posture
  Who They Are              -- background, what they value, how to show up
  Firm Snapshot             -- fund facts (founded / AUM / stages / sectors)
  Fit Assessment            -- per-criterion verdict table
  Pitch Framing             -- lead, emphasis, founder talk-tracks
  Topics To Handle          -- 5-8 partner-specific concerns
  Anticipated Questions     -- 6-10 partner-specific likely questions
  Closing Posture           -- ask shape, lead-vs-syndicate framing
  Sources                   -- separate verified vs live-research

Hard rules (mirrored from earlier sessions):
- Every partner-specific claim must cite a verified signal_id where
  one exists; sector_norm or null citations call out that the claim
  is generic.
- `insufficient_evidence=True` + populated partner-specific sections
  is rejected at the schema layer.
- `meeting_role` names the dynamic the founder should expect (lead,
  syndicate, strategic-helper, etc.) so the dossier reads as a
  meeting plan, not a CRM summary.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Posture options the LLM picks for the dossier header so the founder
# walks in knowing the kind of meeting this is. Closed list because
# the renderer + downstream consumers branch on it; free-text would
# rot.
MeetingRole = Literal[
    "lead_investor",
    "specialist",          # check participates, partner has domain depth
    "syndicate_participant",
    "strategic_helper",    # not a check, but a useful conversation
    "too_early",
    "too_late",
    "not_fit",
    "unknown",
]

FitVerdict = Literal["strong", "neutral", "weak", "unknown"]


class FitRow(BaseModel):
    criterion: str = Field(..., min_length=1)
    verdict: FitVerdict
    notes: str = Field(..., min_length=1)


class TopicToHandle(BaseModel):
    topic: str = Field(..., min_length=1)
    why_they_care: str = Field(..., min_length=1)
    how_to_answer: str = Field(..., min_length=1)
    # Signal ids the topic draws from. Empty list is allowed (the
    # topic is a generic sector norm), but the renderer surfaces
    # citations when present so the founder can audit.
    citing_signal_ids: list[int] = Field(default_factory=list)


class AnticipatedQuestion(BaseModel):
    question: str = Field(..., min_length=1)
    suggested_answer_direction: str = Field(..., min_length=1)
    # WHY this question is likely from THIS partner -- the dossier's
    # discriminating feature vs a generic VC playbook. Empty when the
    # question is a generic sector norm (rare; the prompt strongly
    # discourages it).
    partner_specific_basis: str = Field(..., min_length=1)
    citing_signal_ids: list[int] = Field(default_factory=list)


class InvestorDossier(BaseModel):
    """Top-level dossier the LLM produces in one call."""

    partner_id: str = Field(..., min_length=1)

    # --- Header ---------------------------------------------------------
    partner_name: str = ""
    partner_role: str = ""           # e.g. "General Partner"
    fund_name: str = ""
    former_roles: list[str] = Field(default_factory=list)
    location: str = ""
    investment_themes: list[str] = Field(default_factory=list)

    # --- Profile Summary ------------------------------------------------
    profile_summary: str = ""        # 2-4 paragraphs, markdown OK
    meeting_role: MeetingRole = "unknown"

    # --- Who They Are And How They Think --------------------------------
    background: str = ""
    operator_investor_pattern: str = ""
    portfolio_advisory_pattern: str = ""
    what_they_value: list[str] = Field(default_factory=list)
    how_to_show_up: list[str] = Field(default_factory=list)

    # --- Firm Snapshot --------------------------------------------------
    firm_founded: str = ""
    firm_aum: str = ""
    firm_stage_focus: str = ""
    firm_check_size: str = ""
    firm_sectors: list[str] = Field(default_factory=list)
    firm_investment_model: str = ""
    firm_lp_network: str = ""
    firm_recent_context: str = ""

    # --- Fit Assessment -------------------------------------------------
    fit_assessment: list[FitRow] = Field(default_factory=list)

    # --- Pitch Framing --------------------------------------------------
    lead_with_paragraph: str = ""          # founder voice
    why_thesis_match: str = ""
    what_to_emphasize: list[str] = Field(default_factory=list)
    what_not_to_overemphasize: list[str] = Field(default_factory=list)
    founder_language: list[str] = Field(default_factory=list)

    # --- Topics To Handle Carefully -------------------------------------
    topics_to_handle: list[TopicToHandle] = Field(default_factory=list)

    # --- Anticipated Questions ------------------------------------------
    anticipated_questions: list[AnticipatedQuestion] = Field(default_factory=list)

    # --- Closing Posture ------------------------------------------------
    next_step_ask: str = ""
    lead_vs_syndicate_frame: str = ""
    process_ask: str = ""
    partner_specific_help_ask: str = ""
    if_too_early_framing: str = ""

    # --- Sources / meta -------------------------------------------------
    # Signal ids the dossier as a whole draws from. The renderer uses
    # this to write the "Sources" section, separating verified app
    # signals from live-research sources when --live-research is on.
    citing_signal_ids: list[int] = Field(default_factory=list)
    live_research_source_urls: list[str] = Field(default_factory=list)
    style_sample_used: bool = False

    # Evidence-gap honesty. The dossier may flag specific weaknesses
    # so the founder knows what's NOT in the app evidence (e.g. no
    # recent public commentary -> the Anticipated Questions section
    # is generic).
    insufficient_evidence: bool = False
    evidence_gaps: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def shape_matches_evidence_flag(self) -> "InvestorDossier":
        """If we declared the evidence insufficient, we must NOT have
        emitted partner-specific topics or anticipated questions --
        those would be fabricated. The escape hatch is the
        evidence_gaps list, which surfaces honestly what's missing.

        Symmetric: when evidence_is sufficient, the topics + questions
        lists must contain at least the minimum the prompt asked for.
        """
        if self.insufficient_evidence:
            partner_specific_topics = [
                t for t in self.topics_to_handle if t.citing_signal_ids
            ]
            if partner_specific_topics:
                raise ValueError(
                    "insufficient_evidence=True must not be paired with "
                    "topics that cite signal_ids; either flip the flag "
                    "or drop the unsupported citations"
                )
            if self.anticipated_questions:
                partner_specific_qs = [
                    q for q in self.anticipated_questions
                    if q.citing_signal_ids
                ]
                if partner_specific_qs:
                    raise ValueError(
                        "insufficient_evidence=True must not be paired "
                        "with anticipated_questions that cite signal_ids"
                    )
            return self
        # Evidence sufficient -> require minimum partner-specific content.
        # The prompt's contract: 5-8 topics, 6-10 questions. We enforce
        # at least the lower bounds at the schema layer so a hostile
        # model that produces fewer items has to retry.
        if len(self.topics_to_handle) < 3:
            raise ValueError(
                f"insufficient_evidence=False requires at least 3 "
                f"topics_to_handle (the prompt asks for 5-8); got "
                f"{len(self.topics_to_handle)}"
            )
        if len(self.anticipated_questions) < 4:
            raise ValueError(
                f"insufficient_evidence=False requires at least 4 "
                f"anticipated_questions (the prompt asks for 6-10); "
                f"got {len(self.anticipated_questions)}"
            )
        if not self.lead_with_paragraph.strip():
            raise ValueError(
                "insufficient_evidence=False requires a non-empty "
                "lead_with_paragraph -- empty means the LLM produced "
                "nothing useful for the pitch framing section"
            )
        return self
