"""Pydantic schema for the meeting-prep objection-map builder.

Each `Objection` must be tied to a verified signal (or explicitly
labeled as a generic sector norm). The schema-level enforcement
mirrors the brief's existing discipline: every claim cites evidence,
or it doesn't ship.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ObjectionSource = Literal[
    "stated_thesis",       # partner has stated this position on record
    "portfolio_pattern",   # implied by their portfolio composition / write-offs
    "public_position",     # broader on-record commentary not tied to a deal
    "sector_norm",         # generic VC objection in this sector (no signal needed)
]

ObjectionSeverity = Literal["high", "medium", "low"]


class Objection(BaseModel):
    objection: str = Field(..., min_length=1)
    underlying_concern: str = Field(..., min_length=1)
    source: ObjectionSource
    # Signal ids the objection draws from. MUST be non-empty for any
    # source != sector_norm so the operator can audit the basis.
    citing_signal_ids: list[int] = Field(default_factory=list)
    strong_answer_hint: str = Field(..., min_length=1)
    weak_answer_hint: str = Field(..., min_length=1)
    severity: ObjectionSeverity

    @model_validator(mode="after")
    def evidence_required_unless_sector_norm(self) -> "Objection":
        """No invented psychology. If you don't have a quote / pattern
        to point to, label it as a sector_norm so the reader knows
        it's a generic prior, not a partner-specific position."""
        if self.source != "sector_norm" and not self.citing_signal_ids:
            raise ValueError(
                f"objection with source={self.source!r} must cite at "
                f"least one signal_id; use source='sector_norm' for "
                f"generic objections without partner evidence"
            )
        return self


class ObjectionMapV1(BaseModel):
    """Top-level output of `core/meeting_prep/objection_map.py`."""
    partner_id: str = Field(..., min_length=1)
    objections: list[Objection] = Field(default_factory=list)
    # True when the partner has fewer than 2 quality->=2 signals, in
    # which case the builder refuses to fabricate and writes a
    # one-line note instead. Mirrors Gate 5's signal floor.
    insufficient_evidence: bool = False
    notes: str = ""

    @model_validator(mode="after")
    def shape_matches_evidence_flag(self) -> "ObjectionMapV1":
        """If we declared the evidence insufficient, we MUST NOT have
        emitted partner-specific objections -- those would be
        fabricated. The only allowed shape with insufficient_evidence
        is zero objections (or generic sector_norm entries).
        Symmetric: a non-empty list of partner-specific objections
        means the evidence was sufficient."""
        partner_specific = [
            o for o in self.objections if o.source != "sector_norm"
        ]
        if self.insufficient_evidence and partner_specific:
            raise ValueError(
                "insufficient_evidence=True must not be paired with "
                "partner-specific objections; either flip the flag or "
                "drop the unsupported entries"
            )
        if not self.insufficient_evidence and not partner_specific and self.objections:
            # All objections are sector_norm but we said evidence was
            # sufficient -- inconsistent. The whole point of the
            # evidence-sufficient branch is having partner-specific
            # claims.
            raise ValueError(
                "insufficient_evidence=False but no partner-specific "
                "objections were produced; set the flag to True or "
                "add objections tied to signal_ids"
            )
        return self
