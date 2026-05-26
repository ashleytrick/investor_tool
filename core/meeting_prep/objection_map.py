"""Objection-map builder.

Reads verified partner signals + the company.yaml block + the fund's
kill signals, produces a 5-7 item objection map keyed to the partner.
Every objection cites a signal_id (or is explicitly labeled as a
sector_norm). On insufficient signals, returns a clean
'insufficient_evidence' stub instead of fabricating.

Caching: the result is keyed on (partner_id, signal_set_hash). A new
verified signal flips the hash; an unchanged set is a free cache hit.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from sqlalchemy.engine import Engine

from core.llm.client import LLMClient
from core.meeting_prep.cache import hash_signal_set, lookup, write
from core.meeting_prep.evidence import PartnerEvidence, load_evidence
from schemas.objection_map import ObjectionMapV1

ARTIFACT_TYPE = "objection_map"
_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "prompts" / "objection_map.txt"
)


def build(
    *, engine: Engine, llm: LLMClient, partner_id: str,
    company_cfg: dict, force: bool = False,
    stub_response: dict | None = None,
) -> ObjectionMapV1:
    """Return a validated ObjectionMapV1 for `partner_id`.

    Cache flow:
      - Compute the signal_set_hash from the partner's quality>=2 ids.
      - Look it up. Hit -> return parsed payload, zero LLM calls.
      - Miss -> evidence gate -> LLM call -> validate -> cache write.

    `force=True` bypasses the cache hit (still writes a new row on
    success). `stub_response` is wired through to the LLM client for
    offline tests and the no-API-key path.
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
            return ObjectionMapV1.model_validate_json(hit.payload_json)

    # Evidence gate. Below the floor we don't call the LLM at all --
    # the brief's discipline says "no fabrication when signals are
    # thin". Cache the stub so the next call is also free.
    if not ev.has_enough_signals:
        stub = ObjectionMapV1(
            partner_id=partner_id,
            objections=[],
            insufficient_evidence=True,
            notes=(
                f"only {len(ev.quality_signal_ids)} quality>=2 signal(s) "
                f"on file for this partner; "
                f"meeting-prep objection map skipped to avoid fabrication"
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

    prompt = _render_prompt(ev=ev, company_cfg=company_cfg)
    output = llm.complete_json(
        prompt=prompt,
        schema=ObjectionMapV1,
        stub_response=stub_response,
    )

    # Validate signal_id citations: every cited id must exist in the
    # partner's verified set. The schema can't enforce this (it only
    # sees ints) so we check here.
    known = {s["signal_id"] for s in ev.verified_signals}
    for o in output.objections:
        for sid in o.citing_signal_ids:
            if sid not in known:
                raise ValueError(
                    f"objection cites signal_id={sid} which is not a "
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


def _render_prompt(*, ev: PartnerEvidence, company_cfg: dict) -> str:
    """Fill the prompt template from the loaded evidence + the
    operator's company.yaml block. Uses the new flat fields the
    onboarding wizard populates -- problem / solution / differentiators
    / why_now / traction earn their keep here for the first time."""
    c = (company_cfg or {}).get("company") or {}
    rc = (company_cfg or {}).get("raise_context") or {}
    partner = ev.partner_row
    fund = ev.fund_row
    return (
        _PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{PARTNER_ID}", ev.partner_id)
        .replace("{PARTNER_NAME}", partner.name or "")
        .replace("{PARTNER_TITLE}", partner.title or "")
        .replace("{PARTNER_BIO}", partner.bio or "")
        .replace("{FUND_NAME}", (fund.name if fund else "") or "")
        .replace("{FUND_THESIS}", (fund.stated_thesis if fund else "") or "")
        .replace(
            "{FUND_KILL_SIGNALS}",
            (fund.kill_signals if fund and fund.kill_signals else "(none)"),
        )
        .replace("{COMPANY_NAME}", _safe(c, "name"))
        .replace("{COMPANY_ONE_LINER}", _safe(c, "one_liner"))
        .replace("{COMPANY_PROBLEM}", _safe(c, "problem"))
        .replace("{COMPANY_SOLUTION}", _safe(c, "solution"))
        .replace("{COMPANY_DIFFERENTIATORS}", _safe(c, "differentiators"))
        .replace("{COMPANY_WHY_NOW}", _safe(c, "why_now"))
        .replace("{COMPANY_TRACTION}", _safe(c, "traction"))
        .replace(
            "{ROUND_AMOUNT}",
            _money(c.get("round_amount_usd")) or rc.get("amount", ""),
        )
        .replace(
            "{ROUND_INSTRUMENT}",
            c.get("round_instrument") or rc.get("instrument", ""),
        )
        .replace(
            "{ROUND_CLOSE_TARGET}",
            c.get("round_close_target") or rc.get("timing", ""),
        )
        .replace("{SIGNALS_JSON}", json.dumps(ev.verified_signals, default=str))
        .replace(
            "{PARTNER_DEALS_JSON}",
            json.dumps(ev.partner_deals, default=str),
        )
    )


def _safe(d: dict, key: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else ""


def _money(v: Any) -> str:
    if isinstance(v, (int, float)) and v > 0:
        return f"${int(v):,}"
    return ""
