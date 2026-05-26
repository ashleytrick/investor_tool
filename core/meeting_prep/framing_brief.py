"""Framing-brief builder.

Synthesizes verified partner signals + the objection map (already
built) + the company.yaml block into a one-page "how to tell THIS
company's story to THIS partner" recommendation.

Same cache discipline as objection_map: hash the quality>=2 signal
set, look up, build on miss, persist. Same evidence floor: below 2
quality signals, return insufficient_evidence=True without calling
the LLM.

Depends on objection_map.build() being callable first so the
framing brief can see the same objections the founder will be
preparing for.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from sqlalchemy.engine import Engine

from core.llm.client import LLMClient
from core.meeting_prep import objection_map as om
from core.meeting_prep.cache import hash_signal_set, lookup, write
from core.meeting_prep.evidence import PartnerEvidence, load_evidence
from schemas.framing_brief import FramingBriefV1
from schemas.objection_map import ObjectionMapV1

ARTIFACT_TYPE = "framing_brief"
_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "prompts" / "framing_brief.txt"
)


def build(
    *, engine: Engine, llm: LLMClient, partner_id: str,
    company_cfg: dict, force: bool = False,
    stub_response: dict | None = None,
    objection_map_stub: dict | None = None,
) -> FramingBriefV1:
    """Return a validated FramingBriefV1 for `partner_id`.

    Builds (or fetches the cached) objection map first, then feeds it
    into the framing brief prompt. Same cache hit/miss flow as
    `objection_map.build`.

    `objection_map_stub` is forwarded to the objection_map call when
    the LLM is in stub mode (offline tests). `stub_response` is the
    framing-brief stub.
    """
    ev = load_evidence(engine, partner_id)
    if ev is None:
        raise ValueError(f"partner_id {partner_id!r} not found")

    sig_hash = hash_signal_set(ev.quality_signal_ids)

    if not force:
        hit = lookup(
            engine,
            partner_id=partner_id,
            artifact_type=ARTIFACT_TYPE,
            signal_set_hash=sig_hash,
        )
        if hit is not None:
            return FramingBriefV1.model_validate_json(hit.payload_json)

    if not ev.has_enough_signals:
        stub = FramingBriefV1(
            partner_id=partner_id,
            insufficient_evidence=True,
            notes=(
                f"only {len(ev.quality_signal_ids)} quality>=2 signal(s) "
                f"on file for this partner; "
                f"meeting-prep framing brief skipped to avoid fabrication"
            ),
        )
        write(
            engine,
            partner_id=partner_id,
            artifact_type=ARTIFACT_TYPE,
            signal_set_hash=sig_hash,
            payload_json=stub.model_dump_json(),
            insufficient_evidence=True,
            model_used=None,
        )
        return stub

    # Build (or fetch cached) the objection map first so the framing
    # brief sees the same threat list the founder will be preparing
    # for. We deliberately do NOT propagate `force` here -- the
    # objection map is an input, and if the caller already
    # force-rebuilt it (the typical prep_brief.py flow), the
    # signal-set hash now matches a freshly-written row and we get
    # a clean cache hit. Propagating force would double-write the
    # objection map on every framing rebuild.
    obj_map = om.build(
        engine=engine, llm=llm, partner_id=partner_id,
        company_cfg=company_cfg, force=False,
        stub_response=objection_map_stub,
    )

    prompt = _render_prompt(
        ev=ev, company_cfg=company_cfg, obj_map=obj_map,
    )
    output = llm.complete_json(
        prompt=prompt,
        schema=FramingBriefV1,
        stub_response=stub_response,
    )

    # Same signal_id citation check the objection-map builder does.
    known = {s["signal_id"] for s in ev.verified_signals}
    for sid in output.citing_signal_ids:
        if sid not in known:
            raise ValueError(
                f"framing brief cites signal_id={sid} which is not a "
                f"verified signal for partner {partner_id}; "
                f"known: {sorted(known)}"
            )

    write(
        engine,
        partner_id=partner_id,
        artifact_type=ARTIFACT_TYPE,
        signal_set_hash=sig_hash,
        payload_json=output.model_dump_json(),
        insufficient_evidence=output.insufficient_evidence,
        model_used=getattr(llm, "_last_model_used", None),
    )
    return output


def _render_prompt(
    *, ev: PartnerEvidence, company_cfg: dict, obj_map: ObjectionMapV1,
) -> str:
    c = (company_cfg or {}).get("company") or {}
    partner = ev.partner_row
    fund = ev.fund_row

    def _list(key: str) -> str:
        v = c.get(key)
        if isinstance(v, list) and v:
            return ", ".join(str(x) for x in v)
        return "(none stated)"

    return (
        _PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{PARTNER_ID}", ev.partner_id)
        .replace("{PARTNER_NAME}", partner.name or "")
        .replace("{PARTNER_TITLE}", partner.title or "")
        .replace("{PARTNER_BIO}", partner.bio or "")
        .replace("{FUND_NAME}", (fund.name if fund else "") or "")
        .replace("{FUND_THESIS}", (fund.stated_thesis if fund else "") or "")
        .replace("{COMPANY_NAME}", _safe(c, "name"))
        .replace("{COMPANY_ONE_LINER}", _safe(c, "one_liner"))
        .replace("{COMPANY_PROBLEM}", _safe(c, "problem"))
        .replace("{COMPANY_SOLUTION}", _safe(c, "solution"))
        .replace("{COMPANY_DIFFERENTIATORS}", _safe(c, "differentiators"))
        .replace("{COMPANY_WHY_NOW}", _safe(c, "why_now"))
        .replace("{COMPANY_TRACTION}", _safe(c, "traction"))
        .replace("{COMPANY_DESIRED_TRAITS}", _list("desired_traits"))
        .replace("{COMPANY_EXCLUDED_SECTORS}", _list("excluded_sectors"))
        .replace("{SIGNALS_JSON}", json.dumps(ev.verified_signals, default=str))
        .replace("{OBJECTION_MAP_JSON}", obj_map.model_dump_json())
    )


def _safe(d: dict, key: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else ""
