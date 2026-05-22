"""Stage 7: generate emails + write CSV review queue.

SESSION 1 MINIMAL VERSION. Reads one partner from a fixture, runs it through
stubbed verification/quality/scoring, generates a draft via the LLM client
(stub mode), and writes one CSV row. Session 7 replaces this with the full
strategy-eligibility / two-variant / batch-QA implementation.

Run: uv run scripts/07_generate_emails.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, datetime, timezone

# Make repo-root packages (core, schemas) importable when run as a script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete

from core.config_loader import add_workspace_arg, load_workspace
from core.csv_export import write_review_queue
from core.db import (
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    get_engine,
    partner_score_summaries,
    partners,
    signals,
    upsert,
)
from core.lead_likelihood import compute_lead_likelihood
from core.llm.client import MODEL_EMAIL, LLMClient
from core.round_fit import compute_round_fit
from core.runs import RunLogger
from core.signal_quality import score_signal
from core.verification import verify_signal
from schemas.email_generation import EmailOutput

BATCH_ID = "session1"
STAGE = "07_generate_emails"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _signal_recency_bonus(signal_date: date | None) -> float:
    if signal_date is None:
        return 0.0
    days = (date.today() - signal_date).days
    if days <= 90:
        return 2.0
    if days <= 180:
        return 1.0
    return 0.0


def _build_email_stub(cfg: dict, partner_row: dict, signal_row: dict) -> dict:
    """Construct a schema-shaped EmailOutput stub for offline (stub-mode) runs.

    In live mode this would be the validated model output; in stub mode the
    LLM client validates this same dict so the data shape is proven end to end.
    """
    c = cfg["company"]
    raise_ctx = cfg["raise_context"]
    link = c["meeting_ask"]["preferred_scheduling_link"]
    duration = c["meeting_ask"]["duration_minutes"]
    recommended_body = (
        f"On the Distribution podcast you said compliance reporting is the wedge "
        f"nobody wants to build but everyone regulated has to buy. "
        f"{c['name']} turns regulatory reporting into an API that ships in days, "
        f"with 128% net revenue retention across our first 4 design partners. "
        f"New state mandates land this quarter, so regulated fintechs are in a "
        f"forced-buy window now. We are raising a {raise_ctx['amount']} Seed with "
        f"first close in 8 weeks and I want to book {duration} minutes to walk you "
        f"through the company and round: {link}"
    )
    alt_body = (
        f"{c['name']} has reached $180K ARR with 128% net revenue retention across "
        f"4 paying design partners in regulatory reporting, a category where churn "
        f"is normally high. New state mandates this quarter turn build into buy for "
        f"regulated fintechs. We are raising a $3M Seed closing in 8 weeks and I want "
        f"to book {duration} minutes to walk you through the company and round: {link}"
    )
    return {
        "variants": [
            {
                "strategy": "signal_led",
                "subject": "Regulated reporting, forced buy",
                "body": recommended_body,
                "conversion_hypothesis": (
                    "Anand has publicly framed compliance reporting as a wedge, so a "
                    "company that already monetizes that wedge maps directly to a "
                    "stated belief and a deployable seed thesis."
                ),
                "likely_objection": "Too early; only 4 design partners.",
                "objection_preempted": True,
                "preemption_line": (
                    "with 128% net revenue retention across our first 4 design partners"
                ),
                "template_smell": "unscored",
            },
            {
                "strategy": "traction_led",
                "subject": "Tendril seed, 128% NRR",
                "body": alt_body,
                "conversion_hypothesis": (
                    "Retention is the cleanest seed-stage proof and a metrics-oriented "
                    "investor can underwrite it without a category debate."
                ),
                "likely_objection": "Small absolute ARR.",
                "objection_preempted": True,
                "preemption_line": "128% net revenue retention",
                "template_smell": "unscored",
            },
        ],
        "recommended_variant_strategy": "signal_led",
        "recommendation_reasoning": (
            "The verified podcast quote is a quality-3 signal that gives the strongest, "
            "most partner-specific opener."
        ),
        "limited_variation": False,
        "limited_variation_reason": None,
        "deck_request_response": (
            "Deck attached. The reporting regimes that matter most to you depend on "
            "your portfolio's exposure, so 30 minutes lets me show the parts that are "
            "actually relevant rather than the generic version. Happy to do that "
            f"this week: {c['meeting_ask']['preferred_scheduling_link']}"
        ),
        "followup_draft": (
            "Following up: we signed a fifth design partner this week and first close "
            "is now 6 weeks out. Still worth 30 minutes to walk you through the round?"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 (Session 1 minimal).")
    add_workspace_arg(parser)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)

    fixture_path = ws.fixtures_dir / "session1_fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fund = fixture["fund"]
    partner = fixture["partner"]
    signal = fixture["signal"]

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        run.processed = 1
        try:
            partner_id = partner["partner_id"]

            # --- Persist fund + partner (idempotent upserts) ---
            with engine.begin() as conn:
                upsert(conn, funds, ["fund_id"], {
                    "fund_id": fund["fund_id"],
                    "name": fund["name"],
                    "domain": fund["domain"],
                    "stated_thesis": fund["stated_thesis"],
                    "stated_stage_focus": fund["stated_stage_focus"],
                    "check_size_range": fund["check_size_range"],
                    "last_known_activity_date": date.fromisoformat(
                        fund["last_known_activity_date"]),
                    "is_active": fund["is_active"],
                    "kill_signals": fund["kill_signals"],
                    "source_urls": fund["source_urls"],
                    "last_updated": _now(),
                })
                upsert(conn, partners, ["partner_id"], {
                    "partner_id": partner_id,
                    "fund_id": partner["fund_id"],
                    "name": partner["name"],
                    "title": partner["title"],
                    "linkedin_url": partner["linkedin_url"],
                    "twitter_handle": partner["twitter_handle"],
                    "bio": partner["bio"],
                    "employment_status": partner["employment_status"],
                    "last_updated": _now(),
                })

            # --- Signal: verify (stub) + quality (stub), idempotent insert ---
            ver = verify_signal(signal["source_url"], signal["quoted_text"])
            qual = score_signal(signal["quoted_text"], signal["axis_relevance"])
            signal_date = date.fromisoformat(signal["quote_date"])
            with engine.begin() as conn:
                conn.execute(
                    delete(signals).where(
                        signals.c.partner_id == partner_id,
                        signals.c.source_url == signal["source_url"],
                    )
                )
                conn.execute(signals.insert().values(
                    partner_id=partner_id,
                    source_type=signal["source_type"],
                    source_url=signal["source_url"],
                    quoted_text=signal["quoted_text"],
                    quote_date=signal_date,
                    axis_relevance=json.dumps(signal["axis_relevance"]),
                    signal_direction=signal["signal_direction"],
                    verified=ver.verified,
                    verification_method=ver.verification_method,
                    verification_error=ver.verification_error,
                    signal_quality_score=qual.signal_quality_score,
                    quality_reasoning=qual.quality_reasoning,
                    captured_at=_now(),
                ))

            # --- Deterministic-ish scoring (stubs return canned values) ---
            rf = compute_round_fit(fund, partner, ws.company)
            ll = compute_lead_likelihood(partner, [])
            # Canned 4-axis composite for the slice (Session 6 builds the real one).
            axis_scores = [8.0, 7.0, 5.0, 6.0]
            composite = sum(axis_scores) / len(axis_scores)
            axis_max = max(axis_scores)
            mean = composite
            variance = sum((s - mean) ** 2 for s in axis_scores) / len(axis_scores)
            spiky = max(0.0, min(2.0, variance * 0.5))
            cold_reach = 7.0  # canned; Session 4/6 compute the real value

            verified_quality2_count = (
                1 if (ver.verified and qual.signal_quality_score >= 2) else 0
            )
            recency_bonus = _signal_recency_bonus(signal_date)
            kill_penalty = 0.0
            send_now = (
                rf.round_fit_score * 2.0
                + ll.lead_likelihood_score * 1.5
                + composite * 1.0
                + cold_reach * 0.5
                + recency_bonus
                + spiky
                - kill_penalty
            )

            # --- recommended_to_send (honest evaluation of available criteria) ---
            reasons: list[str] = []
            ok = True
            if composite < 6.5:
                ok = False
                reasons.append("composite_fit_score < 6.5")
            if rf.round_fit_score < 6.0 or rf.disqualifier_present:
                ok = False
                reasons.append("round_fit < 6.0 or disqualifier present")
            if ll.lead_likelihood_score < 5.0:
                ok = False
                reasons.append("lead_likelihood < 5.0")
            if verified_quality2_count < 2:
                ok = False
                reasons.append(
                    "fewer than 2 distinct verified quality>=2 evidence sources "
                    "(Session 1 fixture supplies only one signal)"
                )
            if partner["employment_status"] not in ("verified_current", "likely_current"):
                ok = False
                reasons.append("employment not verified/likely current")
            recommended = ok
            rec_reasoning = (
                "All available recommend-to-send criteria met."
                if ok
                else "Not recommended: " + "; ".join(reasons)
            )

            with engine.begin() as conn:
                upsert(conn, partner_score_summaries, ["partner_id"], {
                    "partner_id": partner_id,
                    "composite_fit_score": composite,
                    "axis_max_score": axis_max,
                    "axis_score_variance": variance,
                    "spiky_belief_score": spiky,
                    "score_confidence": "low",
                    "verified_signal_count": 1 if ver.verified else 0,
                    "quality_2_plus_signal_count": verified_quality2_count,
                    "distinct_source_type_count": 1,
                    "most_recent_signal_date": signal_date,
                    "major_kill_signal_present": False,
                    "kill_signal_summary": "",
                    "cold_reachability_score": cold_reach,
                    "round_fit_score": rf.round_fit_score,
                    "round_fit_reasoning": rf.round_fit_reasoning,
                    "lead_likelihood_score": ll.lead_likelihood_score,
                    "lead_likelihood_signals": ll.lead_likelihood_signals,
                    "send_now_priority": send_now,
                    "employment_status": partner["employment_status"],
                    "manual_score_override": False,
                    "manual_recommended_override": False,
                    "recommended_to_send": recommended,
                    "recommendation_reasoning": rec_reasoning,
                    "scored_at": _now(),
                })

            # --- Email generation (stub mode validates the canned EmailOutput) ---
            stub = _build_email_stub(ws.company, partner, signal)
            email: EmailOutput = llm.complete_json(
                prompt="[Session 1 minimal: see prompts/generate_email.txt in Session 7]",
                schema=EmailOutput,
                model=MODEL_EMAIL,
                stub_response=stub,
            )

            variants = email.variants
            recommended_v = next(
                v for v in variants
                if v.strategy == email.recommended_variant_strategy
            )
            alternate_v = next(
                (v for v in variants if v.strategy != recommended_v.strategy), None
            )

            with engine.begin() as conn:
                conn.execute(
                    delete(email_drafts).where(email_drafts.c.partner_id == partner_id)
                )
                conn.execute(
                    delete(followup_drafts).where(
                        followup_drafts.c.partner_id == partner_id)
                )
                conn.execute(
                    delete(deck_request_responses).where(
                        deck_request_responses.c.partner_id == partner_id)
                )
                for v in variants:
                    conn.execute(email_drafts.insert().values(
                        partner_id=partner_id,
                        batch_id=BATCH_ID,
                        strategy=v.strategy,
                        subject=v.subject,
                        body=v.body,
                        conversion_hypothesis=v.conversion_hypothesis,
                        likely_objection=v.likely_objection,
                        objection_preempted=v.objection_preempted,
                        preemption_line=v.preemption_line,
                        template_smell=v.template_smell,
                        is_recommended=(v.strategy == recommended_v.strategy),
                        generated_at=_now(),
                        written_to_csv_at=_now(),
                    ))
                conn.execute(followup_drafts.insert().values(
                    partner_id=partner_id, body=email.followup_draft,
                    generated_at=_now(),
                ))
                conn.execute(deck_request_responses.insert().values(
                    partner_id=partner_id, body=email.deck_request_response,
                    generated_at=_now(),
                ))

            # --- CSV row ---
            top_signals = (
                f'"{signal["quoted_text"]}" - {signal["source_url"]} '
                f'({signal["quote_date"]})'
            )
            csv_row = {
                "partner_id": partner_id,
                "partner_name": partner["name"],
                "partner_title": partner["title"],
                "fund_name": fund["name"],
                "fund_domain": fund["domain"],
                "linkedin_url": partner["linkedin_url"],
                "send_now_priority": round(send_now, 2),
                "composite_fit_score": round(composite, 2),
                "round_fit_score": rf.round_fit_score,
                "round_fit_reasoning": rf.round_fit_reasoning,
                "lead_likelihood_score": ll.lead_likelihood_score,
                "lead_likelihood_signals": ll.lead_likelihood_signals,
                "cold_reachability_score": cold_reach,
                "spiky_belief_score": round(spiky, 3),
                "top_signals": top_signals,
                "recommended_to_send": recommended,
                "recommendation_reasoning": rec_reasoning,
                "email_strategy_used": recommended_v.strategy,
                "email_subject_line": recommended_v.subject,
                "outreach_email_draft": recommended_v.body,
                "conversion_hypothesis": recommended_v.conversion_hypothesis,
                "likely_objection": recommended_v.likely_objection,
                "objection_preempted": recommended_v.objection_preempted,
                "email_alternate_strategy": alternate_v.strategy if alternate_v else "",
                "email_draft_alternate": alternate_v.body if alternate_v else "",
                "followup_email_draft": email.followup_draft,
                "deck_request_response": email.deck_request_response,
                "template_smell": recommended_v.template_smell,
                "warm_path_available": "",
                "outreach_status": "ready_to_send" if recommended else "draft",
            }
            out_path = write_review_queue(ws.exports_dir, [csv_row])
            run.succeeded = 1
            print(f"[stage 7] wrote CSV review queue: {out_path}")
            print(f"[stage 7] llm stub mode: {llm.stub}")
        except Exception as exc:  # noqa: BLE001 - logged then re-raised
            run.failed = 1
            run.succeeded = 0
            run.log_error(partner.get("partner_id", "?"), type(exc).__name__, str(exc))
            raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
