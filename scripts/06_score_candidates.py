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

from sqlalchemy import delete, func, select

from core.config_loader import add_workspace_arg
from core.stage_runner import stage_run
from core.db import (
    deal_attributions,
    force_refresh_log,
    funds,
    outcomes,
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
from core.llm.client import LLMClient, MODEL_BATCH
from core.round_fit import compute_round_fit
from schemas.candidate_score import CandidateScore

STAGE = "06_score_candidates"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "score_candidate.txt"
ACTIVITY_WINDOW_DAYS = 540  # ~18 months for round_fit recent-deals window
SIGNAL_RECENCY_180_BONUS_DAYS = 180
SIGNAL_RECENCY_90_BONUS_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------- composite scoring (LLM + stub) -------
# composite_and_spikiness + stub_axis_scores moved to
# core/scoring/composite.py (Refactor item 7/13). Local _stub_axis_scores
# alias keeps the existing call-site name unchanged.
from core.scoring.composite import (  # noqa: E402
    composite_and_spikiness,
    stub_axis_scores as _stub_axis_scores,
)


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


# ------- send_now_priority -------
# Moved to core/scoring/send_now_priority.py (Refactor item 7/13).
from core.scoring.send_now_priority import (  # noqa: E402,F401
    compute_send_now_priority,
    signal_recency_bonus,
)


# ------- recommended_to_send (criteria 1-9) -------
# Moved to core/scoring/recommendation.py (Refactor item 7 / 13). Re-
# exported here so existing importlib-based tests that load the Stage 6
# module and call `s6.evaluate_recommended(...)` keep working without
# churn during the extraction window.
from core.scoring.recommendation import evaluate_recommended  # noqa: E402,F401


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

    # Refactor sweep: stage_run() boilerplate collapse.
    with stage_run(args, stage=STAGE) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm
        today = date.today()
        # Batch 39 (#24/#25): scoring config knobs. recent_outreach_window_days
        # tunes how long a fresh active-outreach outcome suppresses re-
        # recommendation (default 30). min_deal_confidence filters out Stage
        # 3 attributions below the threshold from counting as deal evidence
        # (default 0.0 = keep all).
        scoring_cfg = (ws.company or {}).get("scoring") or {}
        recent_outreach_window_days = int(
            scoring_cfg.get("recent_outreach_window_days", 30)
        )
        min_deal_confidence = float(
            scoring_cfg.get("min_deal_confidence", 0.0)
        )

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
            # Batch 39 (#25): apply min_deal_confidence filter so low-
            # confidence Stage 3 fuzzy attributions don't count as evidence.
            # Rows pre-dating the match_confidence column (NULL value) are
            # KEPT for backward compat -- only explicitly-low rows get
            # filtered.
            partner_deals: dict[str, list[dict]] = {}
            filtered_low_confidence = 0
            for d in conn.execute(
                select(deal_attributions).where(
                    deal_attributions.c.attributed_partner_id.isnot(None)
                )
            ):
                if (
                    min_deal_confidence > 0.0
                    and d.match_confidence is not None
                    and d.match_confidence < min_deal_confidence
                ):
                    filtered_low_confidence += 1
                    continue
                partner_deals.setdefault(d.attributed_partner_id, []).append({
                    "company": d.company,
                    "round_type": d.round_type,
                    "round_size_usd": d.round_size_usd,
                    "announcement_date": d.announcement_date,
                    "source_url": d.source_url,
                })
            # Batch 19: latest outcome per partner so evaluate_recommended can
            # suppress re-outreach when a partner is in active or terminal
            # outreach state. Iterate ascending so the LAST iteration wins =
            # most recent by synced_from_attio_at (with outcome_id as tiebreak).
            latest_outcome_by_partner: dict[str, dict] = {}
            for o in conn.execute(
                select(outcomes).order_by(
                    outcomes.c.synced_from_attio_at, outcomes.c.outcome_id,
                )
            ):
                latest_outcome_by_partner[o.partner_id] = {
                    "outreach_status": o.outreach_status,
                    "reply_type": o.reply_type,
                    "meeting_booked": o.meeting_booked,
                    "meeting_date": o.meeting_date,
                    "meeting_outcome": o.meeting_outcome,
                    "synced_from_attio_at": o.synced_from_attio_at,
                    "source": o.source,
                }

        cutoff_18mo = today - timedelta(days=ACTIVITY_WINDOW_DAYS)

        partner_id_filter = set(args.partner_id) if args.partner_id else None

        recommended_count = 0
        for p in partner_rows:
            with run.attempt():
                if partner_id_filter and p.partner_id not in partner_id_filter:
                    run.skip()
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
                    run.skip()
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
                        run.skip()
                        continue
                    fund = fund_rows.get(p.fund_id)
                    if fund is None:
                        run.skip()
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

                    # cold_reachability_score: combines Stage-4 LLM partial
                    # with deterministic post count + recency bands.
                    # core/scoring/reachability.py owns the formula (Refactor
                    # 7/13). Brief Step 4 also lists a contact-info component
                    # derived from fund-site scraping; that lands when Stage 2
                    # enrichment persists contact-info presence.
                    from core.scoring.reachability import (
                        compute_cold_reachability,
                    )
                    # also need most_recent for signal_recency_bonus below
                    most_recent = max(
                        (s["date"] for s in p_signals if s.get("date")),
                        default=None,
                    )
                    cold_reachability = compute_cold_reachability(
                        partial_score=p.cold_reachability_partial_score,
                        partner_signals=p_signals,
                        today=today,
                    )

                    # Major-kill aggregation owned by
                    # core/scoring/major_kill.py (Refactor 7/13).
                    from core.scoring.major_kill import aggregate_major_kill
                    mk = aggregate_major_kill(
                        round_fit_result=rf, fund=fund, partner=p,
                    )
                    major_kill = mk.present
                    kill_summary = mk.summary

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
                        latest_outcome=latest_outcome_by_partner.get(p.partner_id),
                        latest_outcome_window_days=recent_outreach_window_days,
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

                    print(
                        f"[stage 6] {p.name}: composite={composite} "
                        f"round_fit={rf.round_fit_score:.1f} "
                        f"lead={ll.lead_likelihood_score:.1f} "
                        f"reach={cold_reachability} "
                        f"send_now={send_now:.2f} "
                        f"recommended={recommended}"
                    )
                except Exception as exc:  # noqa: BLE001 - logged, continue
                    run.fail(p.partner_id, type(exc).__name__, str(exc))

        # Batch 28 (#358/#359/#360): when --partner-id was used, the
        # legacy summary line said "N partners recommended_to_send" using
        # `recommended_count` which only counted partners scored THIS RUN.
        # That number is confusingly small when a filter was applied.
        # Now we report both a run-scoped count AND the global total from
        # the table, and we mark filtered runs explicitly so the audit
        # is unambiguous.
        if partner_id_filter:
            with engine.begin() as conn:
                total_recommended = conn.execute(
                    select(func.count()).select_from(partner_score_summaries)
                    .where(partner_score_summaries.c.recommended_to_send.is_(True))
                ).scalar() or 0
            print(
                f"[stage 6] FILTER MODE (--partner-id): scored "
                f"{run.succeeded} of {len(partner_id_filter)} requested "
                f"partner(s); {recommended_count} of those are now "
                f"recommended_to_send. Workspace total: "
                f"{total_recommended} recommended."
            )
            run.note(
                f"filter mode: requested={len(partner_id_filter)} "
                f"scored={run.succeeded} run_recommended={recommended_count} "
                f"workspace_recommended={total_recommended}"
            )
        else:
            print(
                f"[stage 6] {recommended_count} partners recommended_to_send "
                f"(criteria 1-9; Stage 7 finalizes)"
            )
        # Batch 39 (#25): surface low-confidence deal filter count.
        if filtered_low_confidence:
            msg = (
                f"filtered {filtered_low_confidence} deal_attributions "
                f"row(s) below min_deal_confidence={min_deal_confidence}"
            )
            print(f"[stage 6] {msg}")
            run.note(msg)
        # Batch 39 (#28): force-rescore is loud + auditable.
        if args.force_rescore:
            msg = (
                f"FORCE_RESCORE applied: reason={args.reason!r} "
                f"(override-protected partners were rewritten)"
            )
            run.note(msg)
        print(f"[stage 6] llm stub mode: {llm.stub}")
        # Batch 11 (#357): previously returned 0 even when per-partner
        # exceptions had landed in run.failed -- cron / wrapping scripts
        # never noticed partial scoring failures. ctx.exit_code now
        # surfaces run.failed > 0 as exit 2 automatically.
        # Batch 36 (#29): if the run processed partners but EVERY one was
        # skipped (no qualifying signals across the board), the scoring
        # pass produced nothing usable. That's almost always a Stage 4
        # / Stage 5 upstream problem the operator needs to see -- treat
        # as failure so cron / wrappers notice. Filter-mode runs are
        # excluded (they intentionally process a subset).
        if (
            not partner_id_filter
            and run.processed > 0
            and run.succeeded == 0
            and run.failed == 0
        ):
            msg = (
                f"every partner skipped (no qualifying signals); "
                f"this usually means Stage 4/5 produced no verified "
                f"quality>=2 signals. Check Stage 5's verification rate "
                f"and re-run Stage 4 if needed."
            )
            print(f"[stage 6] {msg}")
            run.note(msg)
            run.failed = 1

    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
