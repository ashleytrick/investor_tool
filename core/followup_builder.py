"""FR-5: daily follow-up draft generation.

Walks every active sequence whose `next_touch_due_at` has elapsed
and isn't already at max_touches, and writes a `follow_up_drafts`
row (status='draft') so the Today queue's `follow_ups` array
(FR-4c) has something real to render.

Triggered by `POST /api/public/hooks/build-follow-ups` (daily
cron). Idempotent: re-running on the same sequence/touch_number
is a no-op via a UNIQUE-shaped existence check.

Stub mode (no ANTHROPIC_API_KEY): the LLM call short-circuits to
a deterministic placeholder body so CI + fixture runs work
without a key. The real follow-up bodies require live LLM.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select

from core.db import (
    cadence_settings,
    cadence_touches,
    email_drafts,
    follow_up_drafts,
    funds,
    get_engine,
    outreach_events,
    partners,
    sequences,
)
from core.llm.client import LLMClient, MODEL_EMAIL
from schemas.followup_generation import FollowUpOutput


PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "prompts" / "generate_followup.txt"
)


@dataclass(frozen=True)
class BuildResult:
    workspace: str
    generated: int  # number of new follow_up_drafts rows written
    skipped_done: int  # sequences at max_touches
    skipped_existing: int  # already had a draft for this touch
    skipped_no_cadence: int  # no cadence_touches row for the next position
    errors: list[str]


def build_follow_ups_for_workspace(ws) -> BuildResult:
    """Daily build. Caller is the hook endpoint -- this function
    is independently testable so the test suite can drive it
    without hitting the FastAPI layer.

    `ws` is a `core.config_loader.Workspace` (used for LLM auth
    via the workspace env). The `path` attribute names the
    workspace dir so we can locate pipeline.db.
    """
    ws_path_str = str(getattr(ws, "path", ws))
    errors: list[str] = []
    try:
        engine = get_engine(f"sqlite:///{ws.path}/data/pipeline.db")
    except Exception as exc:  # noqa: BLE001
        return BuildResult(
            workspace=ws_path_str,
            generated=0,
            skipped_done=0,
            skipped_existing=0,
            skipped_no_cadence=0,
            errors=[f"engine_failed: {exc}"],
        )

    now_naive = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    llm = LLMClient(workspace=ws)
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")

    with engine.begin() as conn:
        # Cadence config -- one row per workspace.
        max_touches, touches_by_position = _load_cadence(conn)
        if max_touches is None:
            return BuildResult(
                workspace=ws_path_str,
                generated=0,
                skipped_done=0,
                skipped_existing=0,
                skipped_no_cadence=0,
                errors=[
                    "no cadence_settings row; operator hasn't "
                    "configured cadence yet"
                ],
            )

        # Sequences due for the next touch.
        due_rows = list(conn.execute(
            select(
                sequences.c.sequence_id,
                sequences.c.partner_id,
                sequences.c.current_touch,
                sequences.c.next_touch_due_at,
                sequences.c.thread_id,
            ).where(
                sequences.c.state == "active",
            )
        ))

    generated = 0
    skipped_done = 0
    skipped_existing = 0
    skipped_no_cadence = 0

    for seq_row in due_rows:
        next_touch = int(seq_row.current_touch) + 1
        # 1) max_touches gate.
        if next_touch > max_touches:
            skipped_done += 1
            continue
        # 2) Due-at gate. NULL means "due now" (just-captured
        # sequences before the daily build job has stamped a due
        # date on them).
        due_at = seq_row.next_touch_due_at
        if due_at is not None and due_at > now_naive:
            continue
        # 3) Cadence touch config for this position.
        cad = touches_by_position.get(next_touch)
        if cad is None:
            skipped_no_cadence += 1
            continue
        # 4) Don't double-generate.
        with engine.begin() as conn:
            existing = conn.execute(
                select(follow_up_drafts.c.follow_up_id).where(
                    follow_up_drafts.c.sequence_id == seq_row.sequence_id,
                    follow_up_drafts.c.touch_number == next_touch,
                )
            ).first()
        if existing is not None:
            skipped_existing += 1
            continue

        # 5) Gather context for the prompt.
        context = _gather_context_for_partner(
            engine, partner_id=seq_row.partner_id, ws=ws,
        )
        days_since = _days_since_last_touch(engine, sequence_row=seq_row)

        # 6) Call the LLM (or stub).
        try:
            output = _generate_one_followup(
                llm=llm,
                prompt_template=prompt_template,
                touch_number=next_touch,
                max_touches=max_touches,
                angle=cad["angle"],
                custom_prompt=cad.get("custom_prompt") or "",
                days_since_last_touch=days_since,
                ws=ws,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"sequence_id={seq_row.sequence_id}: llm_failed: {exc}"
            )
            continue

        # 7) Persist.
        with engine.begin() as conn:
            conn.execute(follow_up_drafts.insert().values(
                sequence_id=seq_row.sequence_id,
                touch_number=next_touch,
                angle=cad["angle"],
                why_now=output.rationale,
                variant_index=0,
                subject=(output.subject or None),
                body=output.body,
                status="draft",
                created_at=_dt.datetime.now(_dt.timezone.utc),
            ))
        generated += 1

    return BuildResult(
        workspace=ws_path_str,
        generated=generated,
        skipped_done=skipped_done,
        skipped_existing=skipped_existing,
        skipped_no_cadence=skipped_no_cadence,
        errors=errors,
    )


# ---------- helpers ----------

def _load_cadence(conn) -> tuple[Optional[int], dict[int, dict]]:
    """Return (max_touches, {position: {angle, gap_days, custom_prompt}}).
    max_touches is None when no cadence_settings row exists yet."""
    settings_row = conn.execute(
        select(
            cadence_settings.c.max_touches,
        ).where(cadence_settings.c.key == "default")
    ).first()
    if settings_row is None:
        return None, {}
    max_touches = int(settings_row.max_touches or 4)
    touches = {
        int(r.position): {
            "angle": r.angle,
            "gap_days": int(r.gap_days),
            "custom_prompt": r.custom_prompt,
        }
        for r in conn.execute(select(cadence_touches))
    }
    return max_touches, touches


def _gather_context_for_partner(
    engine, *, partner_id: str, ws,
) -> dict:
    """Pull the partner + fund + previous-touch context the prompt
    needs. Returns a dict ready for `_format_prompt`."""
    with engine.begin() as conn:
        partner_row = conn.execute(
            select(
                partners.c.partner_id,
                partners.c.name,
                partners.c.fund_id,
            ).where(partners.c.partner_id == partner_id)
        ).first()
        fund_name = ""
        if partner_row and partner_row.fund_id:
            f = conn.execute(
                select(funds.c.name).where(
                    funds.c.fund_id == partner_row.fund_id,
                )
            ).first()
            fund_name = f.name if f else ""
        # Previous touch: the most-recent live email_drafts row
        # for this partner (touch 1 lives in email_drafts).
        prev = conn.execute(
            select(
                email_drafts.c.subject, email_drafts.c.body,
            ).where(
                email_drafts.c.partner_id == partner_id,
                email_drafts.c.superseded_at.is_(None),
            ).order_by(email_drafts.c.draft_id.desc())
        ).first()
    company_cfg = getattr(ws, "company", None) or {}
    return {
        "partner_name": partner_row.name if partner_row else "",
        "fund_name": fund_name,
        "previous_subject": (prev.subject if prev else "") or "",
        "previous_body": (prev.body if prev else "") or "",
        "company_cfg": company_cfg,
    }


def _days_since_last_touch(engine, *, sequence_row) -> int:
    """Days between now and the most-recent sent-event for this
    partner. Falls back to a sensible default when no sent event
    exists yet (operator might mark-sent later)."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        last_sent = conn.execute(
            select(outreach_events.c.occurred_at).where(
                outreach_events.c.partner_id == sequence_row.partner_id,
                outreach_events.c.event_type == "sent",
            ).order_by(outreach_events.c.occurred_at.desc())
        ).first()
    if last_sent is None or last_sent.occurred_at is None:
        return 0
    delta = now - last_sent.occurred_at
    return max(0, int(delta.total_seconds() // 86400))


def _format_prompt(
    template: str, *,
    touch_number: int, max_touches: int,
    angle: str, custom_prompt: str,
    days_since_last_touch: int,
    context: dict,
) -> str:
    co = (context["company_cfg"].get("company") or {})
    rc = (context["company_cfg"].get("raise_context") or {})
    fv = (context["company_cfg"].get("founder_voice") or {})
    return (
        template
        .replace("{COMPANY_NAME}", co.get("name", ""))
        .replace("{FOUNDER_NAME}", co.get("founder_name", ""))
        .replace("{PARTNER_NAME}", context["partner_name"])
        .replace("{FUND_NAME}", context["fund_name"])
        .replace("{TOUCH_NUMBER}", str(touch_number))
        .replace("{MAX_TOUCHES}", str(max_touches))
        .replace(
            "{DAYS_SINCE_LAST_TOUCH}", str(days_since_last_touch),
        )
        .replace("{ANGLE}", angle)
        .replace("{CUSTOM_PROMPT}", custom_prompt or "")
        .replace("{PREVIOUS_SUBJECT}", context["previous_subject"])
        .replace("{PREVIOUS_BODY}", context["previous_body"])
        .replace("{ROUND}", rc.get("round", ""))
        .replace("{RAISE_AMOUNT}", rc.get("amount", ""))
        .replace("{RAISE_STATUS}", rc.get("status", ""))
        .replace(
            "{WHY_THIS_ROUND_IS_FUNDABLE_NOW}",
            rc.get("why_now", ""),
        )
        .replace("{FOUNDER_VOICE_STYLE}", fv.get("style", ""))
        .replace(
            "{FOUNDER_BANNED_PHRASES}",
            ", ".join(fv.get("banned_phrases", []) or []),
        )
    )


def _stub_followup_response(
    *, angle: str, touch_number: int, days_since: int,
) -> dict:
    """Deterministic stub for offline / no-LLM-key runs. Real
    follow-up bodies require live LLM; this exists so CI passes
    + the build path is testable end-to-end."""
    angle_bodies = {
        "new_signal": (
            f"Following up on my note from {days_since} days ago. "
            f"A fresh data point: traction is up since I last "
            f"wrote. Worth 15 minutes?"
        ),
        "specific_ask": (
            f"Following up on my note from {days_since} days ago. "
            f"Do you have 15 minutes Tuesday or Wednesday?"
        ),
        "soft_check_in": (
            f"Wanted to make sure my last note didn't get buried. "
            f"Same ask: 15 minutes on the round."
        ),
        "graceful_close": (
            "Last note from me on this. Happy to circle back next "
            "quarter if the timing isn't right now."
        ),
        "custom": (
            f"Following up on my previous note ({days_since} days "
            f"ago). Same meeting ask."
        ),
    }
    return {
        "subject": "",
        "body": angle_bodies.get(angle, angle_bodies["custom"]),
        "rationale": (
            f"touch {touch_number} via angle={angle} after "
            f"{days_since} days of silence"
        ),
        "preempted_objection": None,
    }


def _generate_one_followup(
    *, llm: LLMClient, prompt_template: str,
    touch_number: int, max_touches: int,
    angle: str, custom_prompt: str,
    days_since_last_touch: int,
    ws,
    context: dict,
) -> FollowUpOutput:
    """Build the prompt + call the LLM (or stub)."""
    prompt = _format_prompt(
        prompt_template,
        touch_number=touch_number,
        max_touches=max_touches,
        angle=angle,
        custom_prompt=custom_prompt,
        days_since_last_touch=days_since_last_touch,
        context=context,
    )
    stub = _stub_followup_response(
        angle=angle,
        touch_number=touch_number,
        days_since=days_since_last_touch,
    )
    return llm.complete_json(
        prompt=prompt,
        schema=FollowUpOutput,
        model=MODEL_EMAIL,
        stub_response=stub,
    )
