"""Stage 6: score candidates -- composite, round_fit, lead_likelihood,
cold_reachability, send_now_priority, recommended_to_send.

Per the brief:
  - composite_fit_score is an LLM call (thesis/personality fit ONLY, not round
    eligibility); stub mode falls back to a deterministic per-axis scorer that
    aggregates verified quality->=2 signals per axis.
  - round_fit_score is fully deterministic (core/round_fit.py).
  - lead_likelihood_score is mostly deterministic (core/lead_likelihood.py).
    The LLM never produces the score; the reasoning text is templated.
  - send_now_priority is computed by the formula in the brief.
  - recommended_to_send evaluates criteria 1-9 of the 10-criterion list. The
    Stage-7-only criterion 10 (>=1 strategy with eligibility>=2) is finalized
    in Session 7's full Stage 7. A partner can be downgraded there but not
    upgraded.

Run: uv run scripts/06_score_candidates.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.validate_config import preflight_or_exit
from core.db import (
    deal_attributions,
    force_refresh_log,
    funds,
    get_engine,
    partner_score_summaries,
    partners,
    scores,
    signals,
    upsert,
)

# Fields preserved when manual_score_override is set on a partner.
SCORE_PROTECTED_FIELDS = {
    "composite_fit_score", "axis_max_score", "axis_score_variance",
    "spiky_belief_score", "round_fit_score", "round_fit_reasoning",
    "lead_likelihood_score", "lead_likelihood_signals",
    "cold_reachability_score", "send_now_priority",
}
# Fields preserved when manual_recommended_override is set.
RECOMMENDED_PROTECTED_FIELDS = {
    "recommended_to_send", "recommendation_reasoning",
}
from core.lead_likelihood import compute_lead_likelihood
from core.llm.client import MODEL_BATCH, LLMClient
from core.round_fit import compute_round_fit
from core.runs import RunLogger
from schemas.candidate_score import CandidateScore

STAGE = "06_score_candidates"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "score_candidate.txt"
ACTIVITY_WINDOW_DAYS = 540  # ~18 months for round_fit recent-deals window
SIGNAL_RECENCY_180_BONUS_DAYS = 180
SIGNAL_RECENCY_90_BONUS_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------- composite scoring (LLM + stub) -------

def _stub_axis_scores(verified_signals: list[dict], axes_cfg: dict) -> dict:
    """Deterministic per-axis stub used when the LLM client is offline.

    Signal direction matters: a 'negative' quote on an axis is evidence the
    partner DOES NOT hold that belief, so it should LOWER the axis score, not
    raise it. The previous version counted all signals as positive evidence,
    so an anti-fit quote tagged to the regulated-market axis would bump the
    score for that axis by 0.5-1.0.
    """
    by_axis: dict[str, dict] = {}
    for ax in axes_cfg.get("axes", []):
        ax_id = ax["id"]
        relevant = [s for s in verified_signals if ax_id in s["axes"]]
        if not relevant:
            by_axis[ax_id] = {
                "score": None,
                "supporting_signal_ids": [],
                "confidence": "low",
                "reasoning": "no verified quality>=2 signals on this axis",
            }
            continue
        pos = [s for s in relevant if (s.get("direction") or "").lower() == "positive"]
        neg = [s for s in relevant if (s.get("direction") or "").lower() == "negative"]
        q3 = sum(1 for s in pos if s["quality"] == 3)
        q2 = sum(1 for s in pos if s["quality"] == 2)
        q3_neg = sum(1 for s in neg if s["quality"] == 3)
        q2_neg = sum(1 for s in neg if s["quality"] == 2)
        # Start at 6 (neutral). Positive signals raise it; negative signals
        # subtract proportionally. Clamp to [0, 10] so a partner with
        # several anti-fit quotes lands at 0, not below.
        score = 6.0 + min(3, q3) + (0.5 if q2 else 0.0)
        score -= min(3, q3_neg) + (0.5 if q2_neg else 0.0)
        score = max(0.0, min(10.0, score))
        confidence = "high" if len(relevant) >= 2 else ("medium" if (q3 or q3_neg) else "low")
        by_axis[ax_id] = {
            "score": float(score),
            "supporting_signal_ids": [s["id"] for s in relevant],
            "confidence": confidence,
            "reasoning": (
                f"stub: pos={q3}xQ3+{q2}xQ2, neg={q3_neg}xQ3+{q2_neg}xQ2 "
                f"tagged on this axis"
            ),
        }
    return by_axis


def score_candidate(
    llm: LLMClient,
    *,
    partner_row,
    fund_row,
    verified_signals: list[dict],
    axes_cfg: dict,
    company_cfg: dict,
    round_fit_score: float,
    lead_likelihood_score: float,
) -> CandidateScore:
    stub_response = {"axis_scores": _stub_axis_scores(verified_signals, axes_cfg)}

    # Render axes block + signals JSON for the live LLM path.
    axes_block = "\n".join(
        f'- {ax["id"]} "{ax["name"]}": {ax.get("description","")}'
        for ax in axes_cfg.get("axes", [])
    )
    signals_json = json.dumps([
        {
            "id": s["id"],
            "quote": s["quote"],
            "source_url": s["source_url"],
            "axis_relevance": s["axes"],
            "signal_direction": s["direction"],
            "quality": s["quality"],
            "date": s["date"].isoformat() if s.get("date") else None,
        }
        for s in verified_signals
    ], default=str)
    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{COMPANY_NAME}", company_cfg["company"]["name"])
        .replace("{COMPANY_DESCRIPTION}", company_cfg["company"]["description"])
        .replace("{N_AXES}", str(len(axes_cfg.get("axes", []))))
        .replace("{AXES_BLOCK}", axes_block)
        .replace("{PARTNER_BIO}", partner_row.bio or "")
        .replace("{FUND_THESIS}", fund_row.stated_thesis or "")
        .replace("{SIGNALS_JSON}", signals_json)
        .replace("{ROUND_FIT_SCORE}", f"{round_fit_score:.1f}")
        .replace("{LEAD_LIKELIHOOD_SCORE}", f"{lead_likelihood_score:.1f}")
    )
    return llm.complete_json(
        prompt=prompt,
        schema=CandidateScore,
        model=MODEL_BATCH,
        stub_response=stub_response,
    )


def composite_and_spikiness(
    candidate_score: CandidateScore, axes_cfg: dict
) -> tuple[float | None, float | None, float, float, str]:
    """Returns (composite_or_None, axis_max_or_None, variance, spiky, confidence)."""
    weights_by_id = {ax["id"]: float(ax.get("weight", 1.0)) for ax in axes_cfg["axes"]}
    scored = [
        (ax_id, ax_data)
        for ax_id, ax_data in candidate_score.axis_scores.items()
        if ax_data.score is not None
    ]
    if not scored:
        return None, None, 0.0, 0.0, "low"

    total_w = sum(weights_by_id.get(ax_id, 1.0) for ax_id, _ in scored)
    weighted = sum(
        ax_data.score * weights_by_id.get(ax_id, 1.0) for ax_id, ax_data in scored
    )
    composite = weighted / total_w
    score_values = [ax_data.score for _, ax_data in scored]
    axis_max = max(score_values)
    if len(score_values) > 1:
        mean = sum(score_values) / len(score_values)
        variance = sum((s - mean) ** 2 for s in score_values) / len(score_values)
    else:
        variance = 0.0
    spiky = max(0.0, min(2.0, variance * 0.5))

    n = len(scored)
    confidence = "high" if n >= 4 else ("medium" if n >= 2 else "low")
    return composite, axis_max, variance, spiky, confidence


# ------- send_now_priority -------

def signal_recency_bonus(most_recent: date | None, today: date) -> float:
    # days_since() returns None for missing/future dates, so neither inflates
    # the recency bonus.
    from core.dates import days_since
    days = days_since(most_recent, today)
    if days is None:
        return 0.0
    if days <= SIGNAL_RECENCY_90_BONUS_DAYS:
        return 2.0
    if days <= SIGNAL_RECENCY_180_BONUS_DAYS:
        return 1.0
    return 0.0


def compute_send_now_priority(
    *,
    round_fit_score: float,
    lead_likelihood_score: float,
    composite_fit_score: float | None,
    cold_reachability_score: float,
    spiky_belief_score: float,
    recency_bonus: float,
    major_kill: bool,
) -> float:
    comp = composite_fit_score if composite_fit_score is not None else 0.0
    return (
        round_fit_score * 2.0
        + lead_likelihood_score * 1.5
        + comp * 1.0
        + cold_reachability_score * 0.5
        + recency_bonus
        + spiky_belief_score
        - (10.0 if major_kill else 0.0)
    )


# ------- recommended_to_send (criteria 1-9) -------

def evaluate_recommended(
    *,
    composite: float | None,
    round_fit_score: float,
    disqualifier_present: bool,
    lead_likelihood_score: float | None,
    distinct_source_types: int,
    q2_plus_signal_count: int,
    deal_attribution_count: int,
    most_recent_signal_date: date | None,
    employment_status: str | None,
    major_kill: bool,
    cold_reachability_score: float | None,
    warm_path_available: bool | None,
    today: date,
) -> tuple[bool, str]:
    fails: list[str] = []
    if composite is None or composite < 6.5:
        fails.append(f"composite_fit_score ({composite}) < 6.5")
    if round_fit_score < 6.0 or disqualifier_present:
        fails.append(
            f"round_fit_score ({round_fit_score:.1f}) < 6.0 or disqualifier present"
        )
    if lead_likelihood_score is not None and lead_likelihood_score < 5.0:
        fails.append(f"lead_likelihood_score ({lead_likelihood_score:.1f}) < 5.0")
    # criterion 4: 2 distinct evidence sources at quality>=2
    crit4_ok = (
        distinct_source_types >= 2
        or (q2_plus_signal_count >= 1 and deal_attribution_count >= 1)
    )
    if not crit4_ok:
        fails.append(
            "fewer than 2 distinct verified quality>=2 evidence sources "
            "(need 2 distinct source_types or 1 thesis + 1 deal pattern)"
        )
    # within_days() = past-and-bounded; future-dated quotes don't sneak past.
    from core.dates import within_days as _within
    if not _within(most_recent_signal_date, 540, today):
        fails.append("no verified quality>=2 evidence within last 18 months")
    if employment_status not in ("verified_current", "likely_current"):
        fails.append(f"employment_status={employment_status!r} not current")
    if major_kill:
        fails.append("major kill signal present")
    if cold_reachability_score is not None and cold_reachability_score < 5.0:
        fails.append(
            f"cold_reachability_score ({cold_reachability_score:.1f}) < 5.0"
        )
    if warm_path_available is True:
        fails.append("warm_path_available=TRUE -- prefer warm route")
    ok = not fails
    reasoning = (
        "All Stage 6 criteria met; Stage 7 finalizes (strategy eligibility)."
        if ok
        else "Not recommended: " + "; ".join(fails)
    )
    return ok, reasoning


# ------- main -------

def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 6 candidate scoring.")
    add_workspace_arg(parser)
    parser.add_argument(
        "--force-rescore", action="store_true",
        help="Bypass manual_score_override / manual_recommended_override and "
             "overwrite affected fields. Requires --reason.",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Required with --force-rescore: justification logged per field "
             "change in force_refresh_log.",
    )
    parser.add_argument(
        "--partner-id", action="append", default=None,
        help="Limit scoring to a specific partner_id (repeatable). Pairs well "
             "with --force-rescore for targeted refresh.",
    )
    args = parser.parse_args()
    if args.force_rescore and not args.reason:
        parser.error("--force-rescore requires --reason \"...\"")

    ws = load_workspace(args.workspace)
    preflight_or_exit(ws, stage=STAGE)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)
    today = date.today()

    # ---- load all the data we need in one pass ----
    with engine.begin() as conn:
        partner_rows = list(conn.execute(select(partners)))
        fund_rows = {r.fund_id: r for r in conn.execute(select(funds))}

        # Per-partner: quality>=2 verified signals (used for composite + Stage 7).
        verified_signals_by_partner: dict[str, list[dict]] = {}
        # Per-partner: ALL verified signals incl. quality=1, used only for the
        # honest verified_signal_count we persist for audit. Previously this
        # field was len(verified_signals_by_partner[pid]) which dropped the
        # quality-1 verified signals -- two columns ended up identical.
        all_verified_count_by_partner: dict[str, int] = {}
        for s in conn.execute(
            select(signals).where(signals.c.verified.is_(True))
        ):
            all_verified_count_by_partner[s.partner_id] = (
                all_verified_count_by_partner.get(s.partner_id, 0) + 1
            )
            q = int(s.signal_quality_score or 0)
            if q < 2:
                continue
            verified_signals_by_partner.setdefault(s.partner_id, []).append({
                "id": int(s.signal_id),
                "quote": s.quoted_text,
                "source_url": s.source_url,
                "source_type": s.source_type,
                "axes": json.loads(s.axis_relevance or "[]"),
                "direction": s.signal_direction,
                "quality": q,
                "date": s.quote_date,
            })

        # Per-fund deals (for round_fit recent_relevant_deals + has_led_recently).
        deals_by_fund: dict[str, list[dict]] = {}
        for d in conn.execute(select(deal_attributions)):
            tags_raw = d.sector_tags
            try:
                sector_tags = json.loads(tags_raw) if tags_raw else []
            except (TypeError, ValueError):
                sector_tags = []
            deals_by_fund.setdefault(d.lead_fund_id, []).append({
                "company": d.company,
                "round_type": d.round_type,
                "round_size_usd": d.round_size_usd,
                "announcement_date": d.announcement_date,
                "sector_tags": sector_tags,
                "source_url": d.source_url,
            })
        # Per-partner attributed deals (for lead_likelihood).
        partner_deals: dict[str, list[dict]] = {}
        for d in conn.execute(
            select(deal_attributions).where(
                deal_attributions.c.attributed_partner_id.isnot(None)
            )
        ):
            partner_deals.setdefault(d.attributed_partner_id, []).append({
                "company": d.company,
                "round_type": d.round_type,
                "round_size_usd": d.round_size_usd,
                "announcement_date": d.announcement_date,
                "source_url": d.source_url,
            })

    cutoff_18mo = today - timedelta(days=ACTIVITY_WINDOW_DAYS)

    partner_id_filter = set(args.partner_id) if args.partner_id else None

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        recommended_count = 0
        for p in partner_rows:
            run.processed += 1
            if partner_id_filter and p.partner_id not in partner_id_filter:
                run.skipped += 1
                continue

            # Manual override gate: routine runs never overwrite a partner whose
            # user-set flags are True. --force-rescore --reason bypasses this and
            # logs every changed field to force_refresh_log.
            with engine.begin() as conn:
                existing = conn.execute(
                    select(partner_score_summaries).where(
                        partner_score_summaries.c.partner_id == p.partner_id
                    )
                ).first()
            existing_score_override = bool(
                existing and existing.manual_score_override
            )
            existing_rec_override = bool(
                existing and existing.manual_recommended_override
            )
            if (
                (existing_score_override or existing_rec_override)
                and not args.force_rescore
            ):
                run.skipped += 1
                print(
                    f"[stage 6] {p.partner_id}: manual override set "
                    f"(score={existing_score_override}, "
                    f"recommended={existing_rec_override}); "
                    "skipping. Use --force-rescore --reason \"...\" to overwrite."
                )
                continue

            try:
                p_signals = verified_signals_by_partner.get(p.partner_id, [])
                if not p_signals:
                    # Stale-state invalidation (Findings 1 + 3): if a partner
                    # has no qualifying signals, remove their stale summary
                    # + scores so downstream stages don't carry yesterday's
                    # decision forward. Preserve rows that the operator has
                    # explicitly pinned with a manual override.
                    if existing and not (
                        existing_score_override or existing_rec_override
                    ):
                        with engine.begin() as conn:
                            conn.execute(
                                partner_score_summaries.delete().where(
                                    partner_score_summaries.c.partner_id
                                    == p.partner_id
                                )
                            )
                            conn.execute(
                                scores.delete().where(
                                    scores.c.partner_id == p.partner_id
                                )
                            )
                        run.note(
                            f"invalidated stale summary for {p.partner_id} "
                            f"(no current verified quality>=2 signals)"
                        )
                    run.skipped += 1
                    continue
                fund = fund_rows.get(p.fund_id)
                if fund is None:
                    run.skipped += 1
                    run.log_error(p.partner_id, "no_fund", "fund row missing")
                    continue

                fund_deals = deals_by_fund.get(p.fund_id, [])
                # Bound on BOTH sides: future-dated deals (bad parsing) must
                # not count as recent fund activity.
                fund_deals_18mo = [
                    d for d in fund_deals
                    if d.get("announcement_date")
                    and cutoff_18mo <= d["announcement_date"] <= today
                ]
                fund_has_led_recently = len(fund_deals_18mo) > 0

                # Build context dicts the helpers expect.
                fund_dict = {
                    "stated_stage_focus": fund.stated_stage_focus,
                    "check_size_range": fund.check_size_range,
                    "is_active": bool(fund.is_active),
                }
                partner_dict = {"title": p.title}

                # Stage 2: deterministic round_fit.
                rf = compute_round_fit(
                    fund_dict, partner_dict, fund_deals_18mo,
                    fund_has_led_recently, ws.company,
                )

                # Stage 3: deterministic lead_likelihood.
                ll = compute_lead_likelihood(
                    partner_dict, partner_deals.get(p.partner_id, []), today,
                )

                # Stage 1: composite (LLM or stub).
                cs = score_candidate(
                    llm,
                    partner_row=p,
                    fund_row=fund,
                    verified_signals=p_signals,
                    axes_cfg=ws.axes,
                    company_cfg=ws.company,
                    round_fit_score=rf.round_fit_score,
                    lead_likelihood_score=ll.lead_likelihood_score,
                )
                composite, axis_max, variance, spiky, score_conf = composite_and_spikiness(
                    cs, ws.axes
                )

                # cold_reachability_score: combines Stage-4 LLM partial (weight
                # 0.6 -> max contribution 6.0) with deterministic components:
                # post count in last 12mo (up to 2.0) and recency of last post
                # (up to 2.0). Max 10.0. Brief Step 4 also lists a contact-info
                # component derived from fund-site scraping; that lands when
                # Stage 2 enrichment persists contact-info presence.
                most_recent = max((s["date"] for s in p_signals if s.get("date")),
                                  default=None)
                # Past-only counting: future-dated signals don't pad the
                # 12-month post count or the recency bonus.
                from core.dates import days_since, within_days
                post_count_12mo = sum(
                    1 for s in p_signals
                    if within_days(s.get("date"), 365, today)
                )
                if post_count_12mo >= 3:
                    posts_pts = 2.0
                elif post_count_12mo >= 1:
                    posts_pts = 1.0
                else:
                    posts_pts = 0.0
                _mr = days_since(most_recent, today)
                if _mr is None:
                    recency_pts = 0.0
                elif _mr <= 90:
                    recency_pts = 2.0
                elif _mr <= 180:
                    recency_pts = 1.0
                else:
                    recency_pts = 0.0
                partial = p.cold_reachability_partial_score
                cold_reachability = (
                    max(0.0, min(10.0, float(partial) * 0.6 + posts_pts + recency_pts))
                    if partial is not None else None
                )

                # Major kill signal aggregation. components.get() rather than
                # bracket access so a future shape change in compute_round_fit
                # doesn't KeyError here.
                # fund.kill_signals (Stage 2 LLM extraction) was previously
                # stored but never consulted by Stage 6 -- a fund whose
                # extracted thesis said "pre-seed only" would NOT trigger
                # major_kill unless round_fit also caught it. Treat any
                # non-empty extracted kill_signals string as a kill.
                fund_kill_signals_str = (fund.kill_signals or "").strip()
                fund_has_kill = bool(fund_kill_signals_str)
                major_kill = (
                    rf.disqualifier_present
                    or (p.employment_status == "left_fund")
                    or (
                        not fund.is_active
                        and rf.components.get("active_fund", 0) == 0
                    )
                    or fund_has_kill
                )
                kill_summary_parts: list[str] = []
                if rf.triggered_disqualifiers:
                    kill_summary_parts.extend(rf.triggered_disqualifiers)
                if fund_has_kill:
                    kill_summary_parts.append(
                        f"fund kill_signals: {fund_kill_signals_str}"
                    )
                kill_summary = "; ".join(kill_summary_parts) if major_kill else ""

                recency_bonus = signal_recency_bonus(most_recent, today)
                # Previously: `cold_reachability or 5.0` -- unknown reachability
                # silently inflated send_now_priority by ~2.5 points (0.5 weight
                # * 5.0), pushing partners with NO reachability data ABOVE
                # partners scored low. Treat unknown as 0 so the absence of
                # evidence doesn't masquerade as a mid-tier score.
                send_now = compute_send_now_priority(
                    round_fit_score=rf.round_fit_score,
                    lead_likelihood_score=ll.lead_likelihood_score,
                    composite_fit_score=composite,
                    cold_reachability_score=cold_reachability or 0.0,
                    spiky_belief_score=spiky,
                    recency_bonus=recency_bonus,
                    major_kill=major_kill,
                )

                distinct_source_types = len({s["source_type"] for s in p_signals
                                             if s.get("source_type")})
                deal_count = len(partner_deals.get(p.partner_id, []))
                recommended, rec_reason = evaluate_recommended(
                    composite=composite,
                    round_fit_score=rf.round_fit_score,
                    disqualifier_present=rf.disqualifier_present,
                    lead_likelihood_score=ll.lead_likelihood_score,
                    distinct_source_types=distinct_source_types,
                    q2_plus_signal_count=len(p_signals),
                    deal_attribution_count=deal_count,
                    most_recent_signal_date=most_recent,
                    employment_status=p.employment_status,
                    major_kill=major_kill,
                    cold_reachability_score=cold_reachability,
                    warm_path_available=p.warm_path_available,
                    today=today,
                )

                if recommended:
                    recommended_count += 1

                # ---- build values dict; preserve overridden fields/flags ----
                new_values = {
                    "partner_id": p.partner_id,
                    "composite_fit_score": composite,
                    "axis_max_score": axis_max,
                    "axis_score_variance": variance,
                    "spiky_belief_score": spiky,
                    "score_confidence": score_conf,
                    "verified_signal_count": all_verified_count_by_partner.get(
                        p.partner_id, len(p_signals)
                    ),
                    "quality_2_plus_signal_count": len(p_signals),
                    "distinct_source_type_count": distinct_source_types,
                    "most_recent_signal_date": most_recent,
                    "major_kill_signal_present": major_kill,
                    "kill_signal_summary": kill_summary,
                    "cold_reachability_score": cold_reachability,
                    "round_fit_score": rf.round_fit_score,
                    "round_fit_reasoning": rf.round_fit_reasoning,
                    "lead_likelihood_score": ll.lead_likelihood_score,
                    "lead_likelihood_signals": ll.lead_likelihood_signals,
                    "send_now_priority": send_now,
                    "employment_status": p.employment_status,
                    "recommended_to_send": recommended,
                    "recommendation_reasoning": rec_reason,
                    "scored_at": _now(),
                }
                # Manual override flags + reason are preserved from existing
                # row (never silently reset by routine OR forced runs).
                new_values["manual_score_override"] = existing_score_override
                new_values["manual_recommended_override"] = existing_rec_override
                new_values["manual_override_reason"] = (
                    existing.manual_override_reason if existing else None
                )
                # If --force-rescore reached an overridden record, log every
                # changed field to force_refresh_log before the upsert.
                if args.force_rescore and existing and (
                    existing_score_override or existing_rec_override
                ):
                    with engine.begin() as conn:
                        for field, new_v in new_values.items():
                            if field == "scored_at":
                                continue
                            old_v = getattr(existing, field, None)
                            if old_v != new_v:
                                conn.execute(force_refresh_log.insert().values(
                                    partner_id=p.partner_id,
                                    field_name=field,
                                    old_value=str(old_v),
                                    new_value=str(new_v),
                                    reason=args.reason,
                                    refreshed_at=_now(),
                                ))
                with engine.begin() as conn:
                    upsert(conn, partner_score_summaries, ["partner_id"], new_values)
                    # Replace per-axis scores for this partner.
                    conn.execute(delete(scores).where(scores.c.partner_id == p.partner_id))
                    for ax_id, ax_data in cs.axis_scores.items():
                        if ax_data.score is None:
                            continue
                        conn.execute(scores.insert().values(
                            partner_id=p.partner_id,
                            axis_id=ax_id,
                            score=ax_data.score,
                            supporting_signal_ids=json.dumps(ax_data.supporting_signal_ids),
                            confidence=ax_data.confidence,
                            scored_at=_now(),
                        ))

                run.succeeded += 1
                print(
                    f"[stage 6] {p.name}: composite={composite} "
                    f"round_fit={rf.round_fit_score:.1f} "
                    f"lead={ll.lead_likelihood_score:.1f} "
                    f"reach={cold_reachability} "
                    f"send_now={send_now:.2f} "
                    f"recommended={recommended}"
                )
            except Exception as exc:  # noqa: BLE001 - logged, continue
                run.failed += 1
                run.log_error(p.partner_id, type(exc).__name__, str(exc))

        print(
            f"[stage 6] {recommended_count} partners recommended_to_send "
            f"(criteria 1-9; Stage 7 finalizes)"
        )
        print(f"[stage 6] llm stub mode: {llm.stub}")
        # Batch 11 (#357): previously returned 0 even when per-partner
        # exceptions had landed in run.failed -- cron / wrapping scripts
        # never noticed partial scoring failures.
        any_failed = run.failed > 0

    return 2 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
