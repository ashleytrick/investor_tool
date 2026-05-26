"""Investor Dossier builder (Build Session 14).

Produces the post-reply user-facing artifact: a structured dossier
modeled on the Christo sample. One LLM call emits all sections at
once (header, profile, fit assessment, pitch framing, topics, Q&A,
closing posture) so the dossier reads with a unified voice rather
than as glued-together sub-prompts.

The objection_map + framing_brief modules from Session 12 stay
available for callers who want JUST those sub-artifacts; the dossier
doesn't compose them.

Cache key: (partner_id, 'investor_dossier', signal_set_hash,
company_profile_hash, live_research_hash, style_sample_hash). Any
hash change forces a rebuild.

Eligibility: builds refuse for partners that are not dossier-eligible
unless `force_refresh=True` is passed (operator opt-in). The
attio_outcome_sync + record_outcome producers create review_items
rows; --pending-only mode iterates those and calls build() with the
review_item_id stamped onto the resolving artifact row.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine

from core.llm.client import LLMClient
from core.meeting_prep.cache import (
    hash_company_profile,
    hash_signal_set,
    lookup,
    write,
)
from core.meeting_prep.eligibility import (
    EligibilityResult,
    is_dossier_eligible,
)
from core.meeting_prep.evidence import PartnerEvidence, load_evidence
from schemas.investor_dossier import InvestorDossier

ARTIFACT_TYPE = "investor_dossier"
_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "prompts" / "investor_dossier.txt"
)


class DossierIneligibleError(RuntimeError):
    """Raised when build() is called for a partner that isn't
    dossier-eligible and force_refresh is False. Surface as a
    structured error so prep_brief.py can render a clean message
    rather than dumping a stack trace."""

    def __init__(self, partner_id: str, eligibility: EligibilityResult):
        self.partner_id = partner_id
        self.eligibility = eligibility
        super().__init__(
            f"partner {partner_id} not dossier-eligible: "
            f"{eligibility.reason}"
        )


@dataclass
class DossierResult:
    """Returned from build(). Both the parsed schema object and the
    artifact row id (the latter is what prep_brief.py stamps onto a
    resolved review_items row)."""
    dossier: InvestorDossier
    artifact_id: int | None
    eligibility: EligibilityResult
    cache_hit: bool


def build(
    *, engine: Engine, llm: LLMClient, partner_id: str,
    company_cfg: dict, force_refresh: bool = False,
    live_research: bool = False,
    style_sample_path: pathlib.Path | None = None,
    stub_response: dict | None = None,
    created_by: str | None = None,
) -> DossierResult:
    """Build (or fetch the cached) dossier for `partner_id`.

    Eligibility is enforced unless force_refresh=True. The
    force_refresh path also bypasses the cache so the operator can
    deliberately re-spend LLM time after, e.g., a manual signal
    edit.

    `live_research` and `style_sample_path` are accepted for cache
    hashing today; the actual implementations of fetching live
    research sources + extracting style guidance from a .docx are
    deferred (the corresponding hash components are stable stand-ins
    so the cache key shape is forward-compatible).
    """
    eligibility = is_dossier_eligible(engine, partner_id)
    if not eligibility.eligible and not force_refresh:
        raise DossierIneligibleError(partner_id, eligibility)

    ev = load_evidence(engine, partner_id)
    if ev is None:
        raise ValueError(f"partner_id {partner_id!r} not found")

    sig_hash = hash_signal_set(ev.quality_signal_ids)
    cp_hash = hash_company_profile(company_cfg)
    lr_hash = _hash_live_research(live_research)
    style_hash = _hash_style_sample(style_sample_path)

    if not force_refresh:
        hit = lookup(
            engine,
            partner_id=partner_id,
            artifact_type=ARTIFACT_TYPE,
            signal_set_hash=sig_hash,
            company_profile_hash=cp_hash,
            live_research_hash=lr_hash,
            style_sample_hash=style_hash,
        )
        if hit is not None:
            return DossierResult(
                dossier=InvestorDossier.model_validate_json(hit.payload_json),
                artifact_id=None,  # cache hit -- no new row written
                eligibility=eligibility,
                cache_hit=True,
            )

    prompt = _render_prompt(
        ev=ev, company_cfg=company_cfg,
        style_sample_present=style_sample_path is not None,
    )
    output = llm.complete_json(
        prompt=prompt,
        schema=InvestorDossier,
        stub_response=stub_response,
    )

    # Validate every cited signal_id matches a verified signal we
    # actually have. The schema doesn't see the universe of valid
    # ids; only the builder does. Same discipline as the
    # objection_map / framing_brief builders.
    known = {s["signal_id"] for s in ev.verified_signals}
    for t in output.topics_to_handle:
        for sid in t.citing_signal_ids:
            if sid not in known:
                raise ValueError(
                    f"dossier topic cites signal_id={sid} which is "
                    f"not a verified signal for partner {partner_id}; "
                    f"known: {sorted(known)}"
                )
    for q in output.anticipated_questions:
        for sid in q.citing_signal_ids:
            if sid not in known:
                raise ValueError(
                    f"dossier question cites signal_id={sid} which "
                    f"is not a verified signal for partner "
                    f"{partner_id}; known: {sorted(known)}"
                )

    source_summary = {
        "verified_signal_ids": sorted(ev.quality_signal_ids),
        "partner_deal_count": len(ev.partner_deals),
        "live_research_enabled": live_research,
        "live_research_source_urls": list(output.live_research_source_urls),
        "style_sample_used": output.style_sample_used,
    }

    artifact_id = write(
        engine,
        partner_id=partner_id,
        artifact_type=ARTIFACT_TYPE,
        signal_set_hash=sig_hash,
        company_profile_hash=cp_hash,
        live_research_hash=lr_hash,
        style_sample_hash=style_hash,
        payload_json=output.model_dump_json(),
        # content_markdown is written by the caller after rendering;
        # keep this builder schema-focused. prep_brief.py back-fills
        # it on the row it just got the id for.
        content_markdown=None,
        source_summary_json=json.dumps(source_summary, default=str),
        insufficient_evidence=output.insufficient_evidence,
        model_used=getattr(llm, "_last_model_used", None),
        created_by=created_by,
    )

    return DossierResult(
        dossier=output, artifact_id=artifact_id,
        eligibility=eligibility, cache_hit=False,
    )


def _hash_live_research(live_research: bool) -> str:
    """Stable component for the cache key. Today: just a flag (on /
    off). When the live-research implementation lands, this becomes
    a hash of (urls fetched, content_hashes of those snapshots) so
    a refreshed page invalidates the cache."""
    payload = json.dumps(
        {"enabled": bool(live_research), "version": 1},
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hash_style_sample(path: pathlib.Path | None) -> str:
    """Stable component for the cache key. Today: hash of the bytes
    of the style sample file (or a fixed digest when no sample was
    passed). When --style-sample extraction lands, this stays
    correct because the file bytes are what determine the extracted
    structure."""
    if path is None:
        return hashlib.sha256(b"no_style_sample").hexdigest()
    try:
        data = pathlib.Path(path).read_bytes()
    except (OSError, FileNotFoundError):
        # Missing file -> different hash than "no sample" so the
        # operator sees a cache miss + the builder error rather than
        # a silent skip.
        return hashlib.sha256(
            f"missing:{path}".encode("utf-8")
        ).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _render_prompt(
    *, ev: PartnerEvidence, company_cfg: dict,
    style_sample_present: bool,
) -> str:
    c = (company_cfg or {}).get("company") or {}
    rc = (company_cfg or {}).get("raise_context") or {}
    partner = ev.partner_row
    fund = ev.fund_row

    def _list(d: dict, key: str) -> str:
        v = d.get(key)
        if isinstance(v, list) and v:
            return ", ".join(str(x) for x in v)
        return "(none stated)"

    def _money(v: Any) -> str:
        if isinstance(v, (int, float)) and v > 0:
            return f"${int(v):,}"
        return ""

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
        .replace("{COMPANY_NAME}", _str(c, "name"))
        .replace("{COMPANY_ONE_LINER}", _str(c, "one_liner"))
        .replace("{COMPANY_PROBLEM}", _str(c, "problem"))
        .replace("{COMPANY_SOLUTION}", _str(c, "solution"))
        .replace("{COMPANY_DIFFERENTIATORS}", _str(c, "differentiators"))
        .replace("{COMPANY_WHY_NOW}", _str(c, "why_now"))
        .replace("{COMPANY_TRACTION}", _str(c, "traction"))
        .replace(
            "{ROUND_AMOUNT}",
            _money(c.get("round_amount_usd")) or _str(rc, "amount"),
        )
        .replace(
            "{ROUND_INSTRUMENT}",
            _str(c, "round_instrument") or _str(rc, "instrument"),
        )
        .replace(
            "{ROUND_CLOSE_TARGET}",
            _str(c, "round_close_target") or _str(rc, "timing"),
        )
        .replace("{COMPANY_DESIRED_TRAITS}", _list(c, "desired_traits"))
        .replace("{COMPANY_EXCLUDED_SECTORS}", _list(c, "excluded_sectors"))
        .replace(
            "{SIGNALS_JSON}",
            json.dumps(ev.verified_signals, default=str),
        )
        .replace(
            "{PARTNER_DEALS_JSON}",
            json.dumps(ev.partner_deals, default=str),
        )
        .replace(
            "{STYLE_SAMPLE_FLAG}",
            "yes" if style_sample_present else "no",
        )
    )


def _str(d: dict, key: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else ""
