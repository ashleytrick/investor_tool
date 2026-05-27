"""Email samples router.

Operator-uploaded examples of their own writing voice. Stage 7's
draft generator reads up to N most-recent samples and injects
them into the prompt as a `{OPERATOR_VOICE_SAMPLES}` block so the
LLM mirrors the operator's actual cadence, sentence-length,
register, and signoff.

This is distinct from the per-strategy style anchors in
`prompts/examples/*.md` (which the draft generator reads via
`{EXAMPLES_BLOCK}`). Those anchors are about which OPENING
pattern fits each partner. Voice samples are about HOW the
operator writes.

  GET    /settings/email-samples            list all samples
  POST   /settings/email-samples            add a sample
  DELETE /settings/email-samples/{id}       remove a sample

Per-workspace cap of 10 samples to keep prompt token cost
bounded. Older samples can be deleted to make room.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from core.db import email_samples
from web.deps import _engine_and_ws, require_auth


# Per-workspace cap. Keeps the {OPERATOR_VOICE_SAMPLES} block in
# the email prompt bounded (each sample is up to ~10K chars; 10
# samples + the rest of the prompt sits comfortably under the
# Claude context window).
_MAX_SAMPLES_PER_WORKSPACE = 10

# How many samples Stage 7 injects into the prompt. We store more
# than we inject so the operator can curate; the prompt pulls the
# N most-recent.
_PROMPT_SAMPLE_COUNT = 3


class EmailSampleView(BaseModel):
    sample_id: int
    subject: str | None = None
    body: str
    created_at: str
    updated_at: str


class EmailSampleBody(BaseModel):
    body: str = Field(
        min_length=50, max_length=10_000,
        description=(
            "The actual sent email body, ~50 to 10,000 chars. "
            "Short hand-typed snippets aren't useful as voice "
            "anchors; the LLM needs a few full sentences to "
            "mirror style."
        ),
    )
    subject: str | None = Field(
        default=None, max_length=200,
        description=(
            "Optional context for the operator's own audit log "
            "(\"intro to fintech investor X\"). Not injected into "
            "the prompt."
        ),
    )


router = APIRouter(tags=["settings"])


def _row_to_view(row) -> EmailSampleView:
    return EmailSampleView(
        sample_id=int(row.sample_id),
        subject=row.subject,
        body=row.body,
        created_at=(
            row.created_at.isoformat() if row.created_at else ""
        ),
        updated_at=(
            row.updated_at.isoformat() if row.updated_at else ""
        ),
    )


@router.get(
    "/settings/email-samples",
    response_model=list[EmailSampleView],
    summary="List the operator's uploaded email voice samples",
)
def list_email_samples(
    _auth: None = Depends(require_auth),
) -> list[EmailSampleView]:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(email_samples)
            .order_by(desc(email_samples.c.created_at))
        ))
    return [_row_to_view(r) for r in rows]


@router.post(
    "/settings/email-samples",
    response_model=EmailSampleView,
    summary="Upload an email sample for voice mirroring",
)
def add_email_sample(
    body: EmailSampleBody,
    _auth: None = Depends(require_auth),
) -> EmailSampleView:
    """Adds a sample. Returns 409 if the workspace is already at
    the per-workspace cap -- operator must delete an older sample
    first. (Soft cap protects against accidentally bloating the
    Stage 7 prompt.)"""
    engine, _ = _engine_and_ws()
    now = _dt.datetime.now(_dt.timezone.utc)
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(email_samples)
        ).scalar() or 0
        if n >= _MAX_SAMPLES_PER_WORKSPACE:
            raise HTTPException(
                409,
                f"email-samples cap reached "
                f"({_MAX_SAMPLES_PER_WORKSPACE}); delete an "
                f"older sample first",
            )
        result = conn.execute(email_samples.insert().values(
            subject=(body.subject or None),
            body=body.body.strip(),
            created_at=now,
            updated_at=now,
        ))
        sample_id = int(result.inserted_primary_key[0])
        row = conn.execute(
            select(email_samples).where(
                email_samples.c.sample_id == sample_id,
            )
        ).first()
    return _row_to_view(row)


@router.delete(
    "/settings/email-samples/{sample_id}",
    response_model=dict,
    summary="Remove an email sample",
)
def delete_email_sample(
    sample_id: int,
    _auth: None = Depends(require_auth),
) -> dict:
    engine, _ = _engine_and_ws()
    with engine.begin() as conn:
        existing = conn.execute(
            select(email_samples.c.sample_id).where(
                email_samples.c.sample_id == sample_id,
            )
        ).first()
        if existing is None:
            raise HTTPException(
                404, f"unknown sample_id: {sample_id}",
            )
        conn.execute(
            email_samples.delete().where(
                email_samples.c.sample_id == sample_id,
            )
        )
    return {"deleted_sample_id": sample_id}


# ---------- prompt-side helper (used by Stage 7) ----------

def load_voice_samples_for_prompt(engine) -> str:
    """Return up to _PROMPT_SAMPLE_COUNT most-recent samples
    formatted as a single block ready to drop into the
    `{OPERATOR_VOICE_SAMPLES}` placeholder.

    Returns an empty string when the operator hasn't uploaded
    anything yet -- the prompt template handles the empty case
    by falling back to the founder_voice.style hint.
    """
    with engine.begin() as conn:
        rows = list(conn.execute(
            select(email_samples)
            .order_by(desc(email_samples.c.created_at))
            .limit(_PROMPT_SAMPLE_COUNT)
        ))
    if not rows:
        return ""
    parts = []
    for i, r in enumerate(rows, start=1):
        header = f"--- sample {i}"
        if r.subject:
            header += f" (subject: {r.subject})"
        header += " ---"
        parts.append(f"{header}\n{r.body.strip()}")
    return "\n\n".join(parts)
