"""Stage 7: generate emails + write the CSV review queue.

For the top N partners by send_now_priority (default 25; Gate 5 fixture runs
top 5), per partner:
  1. Score the 6 strategies for eligibility (0-3); a strategy is usable only
     at >= 2, and signal_led specifically requires a quality->=3 signal.
  2. Pick two distinct strategies (or one with limited_variation=true).
  3. Produce two variants, a deck_request_response, a follow-up draft, a
     conversion hypothesis, and a likely-objection + preemption tag.
  4. Validate against schemas/email_generation.py.

After per-partner generation, the batch is QA'd:
  - pairwise body / first-sentence / subject similarity (rapidfuzz token_set)
  - template-smell judge (LLM live, heuristic in stub mode) on each draft
    against its 5 nearest neighbors
  - hard gates (similarity, smell, raise reference, soft CTA, eligibility)
  - warning gates (strategy concentration, CTA repetition, smell distribution)

Outputs:
  - email_drafts / followup_drafts / deck_request_responses rows replaced for
    each partner in the batch
  - one batch_qa_reports row
  - clients/{workspace}/exports/review_queue.csv overwritten

Stub mode: when no ANTHROPIC_API_KEY is resolvable, per-partner stub_response
dicts come from a static EMAIL_BANK keyed on partner_id. The live LLM path is
the same code path; only the stub_response source differs.

Run: uv run scripts/07_generate_emails.py --workspace clients/test_workspace --top 5
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from core.config_loader import add_workspace_arg
from core.stage_runner import stage_run
from core.csv_export import write_review_queue
from core.db import (
    batch_qa_reports,
    deal_attributions,
    deck_request_responses,
    email_drafts,
    followup_drafts,
    funds,
    partner_score_summaries,
    partners,
    signals,
)
from core.llm.client import MODEL_EMAIL
from core.production_guards import production_gate_for_ready_to_send
from core.similarity import first_sentence, ratio_similarity, token_set_similarity
from schemas.email_generation import EmailOutput

STAGE = "07_generate_emails"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "generate_email.txt"

# Batch QA thresholds (brief).
SIM_BODY_HARD = 0.82
SIM_FIRST_HARD = 0.70
SIM_SUBJECT_HARD = 0.75
WARN_STRATEGY_SHARE = 0.35
WARN_FIRST_SENT_SHARE = 0.25
WARN_CTA_SHARE = 0.20
WARN_TEMPLATE_LOW_SHARE = 0.80

# Forbidden phrases (universal + founder-voice banned).
UNIVERSAL_FORBIDDEN = [
    "building the future of", "would love", "circling back",
    "wanted to reach out", "hope this finds you", "quick question",
    "pressure-test", "compare notes", "thesis chat", "get your feedback",
    "synergy", "game-changing", "excited to",
]
SOFT_CTA_PHRASES = [
    "thesis chat", "feedback", "pressure-test", "compare notes",
    "grab coffee", "would love to chat",
]


# ------- strategy eligibility -------

# Keywords in a partner's quoted signal that indicate they care about
# metrics / traction / customer evidence. The brief: traction_led requires
# "strong company traction AND a metrics-oriented partner signal", not just
# strong company traction alone.
METRICS_SIGNAL_KEYWORDS = (
    "metric", "metrics", "arr", "retention", "nrr", "growth", "growing",
    "customers", "revenue", "churn", "conversion", "users", "scale",
    "burn", "design partner", "design partners", "sign-up", "sign-ups",
    "sales", "pipeline",
)


# Batch 24 (#420): keyword matching against thesis text used substring
# `kw in thesis` which produces false positives like "ai" in "stairwell"
# or "art" in "smart". Use word-boundary matching against tokenized
# thesis, handling multi-word sectors ("design partners") by matching
# the phrase as a substring AFTER ensuring the surrounding chars are
# non-word.
import re as _re

_NONWORD_RE = _re.compile(r"\W+")


def _company_primary_domain(company_cfg: dict) -> str | None:
    """Best-effort: extract the company's primary domain from the
    scheduling link (post-redirect host) or fall back to the founder
    email's domain. Returns lowercase host, no port. Used by Stage 7's
    founder-email-alignment check (Batch 37 #35).

    When the scheduling link points at a third-party scheduling service
    (cal.com, calendly.com, etc.) OR an RFC 2606 reserved TLD (.example),
    the link doesn't carry the company's primary domain -- fall through
    to the founder email's domain instead.
    """
    co = (company_cfg or {}).get("company") or {}
    link = (co.get("meeting_ask") or {}).get("preferred_scheduling_link") or ""
    scheduling_hosts = (
        "cal.com", "calendly.com", "savvycal.com", "hubspot.com",
        "google.com", "x.ai", "tldv.io",
    )
    reserved_tlds = (".example", ".test", ".invalid", ".localhost")
    if "://" in link:
        rest = link.split("://", 1)[1]
        for sep in ("/", "?", "#", ":"):
            if sep in rest:
                rest = rest.split(sep, 1)[0]
        rest = rest.strip().lower()
        is_scheduling_service = rest in scheduling_hosts or any(
            rest.endswith(suffix) for suffix in reserved_tlds
        )
        if rest and not is_scheduling_service:
            return rest
        # Scheduling-service host: fall through to founder email below.
    fe = (co.get("founder_email") or "").strip().lower()
    if "@" in fe:
        return fe.split("@", 1)[1] or None
    return None


def _word_boundary_hit(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` bounded by non-word chars
    (or start/end of string). Case-insensitive on the caller side; both
    inputs should already be lower()."""
    if not needle:
        return False
    pattern = _re.compile(
        r"(?:^|\W)" + _re.escape(needle) + r"(?:$|\W)"
    )
    return bool(pattern.search(haystack))


def has_metrics_oriented_signal(p_signals: list[dict]) -> bool:
    """True iff at least one verified signal mentions metrics vocabulary."""
    for s in p_signals:
        text = (s.get("quote") or "").lower()
        if any(kw in text for kw in METRICS_SIGNAL_KEYWORDS):
            return True
    return False


# Axis name/description tokens that mark a "timing / market-shift" belief axis.
# Previously Stage 7 hardcoded axis_4 (the fixture's timing axis) to enable
# market_shift_led -- which broke for any workspace that doesn't put the
# timing-driven axis last. Now we resolve it by inspecting axes.yaml so the
# strategy eligibility is workspace-portable.
MARKET_SHIFT_AXIS_TOKENS = (
    "timing", "market shift", "market-shift", "window", "policy",
    "forced buy", "tailwind", "regulatory",
)


def market_shift_axis_ids(axes_cfg: dict) -> set[str]:
    """Return axis IDs whose name or description signals a timing/market-shift
    belief. Falls back to {} if no axis matches -- callers treat that as
    market_shift_led ineligible, which is the safe default."""
    out: set[str] = set()
    for ax in (axes_cfg or {}).get("axes", []) or []:
        blob = " ".join((
            (ax.get("name") or ""),
            (ax.get("description") or ""),
        )).lower()
        if any(tok in blob for tok in MARKET_SHIFT_AXIS_TOKENS):
            out.add(ax.get("id"))
    return {aid for aid in out if aid}


def has_company_traction(company_cfg: dict) -> bool:
    c = (company_cfg.get("company") or {}).get("current_traction") or {}
    return bool(c.get("headline_metric")) or bool(c.get("secondary_metrics"))


def compute_eligibility(
    has_q3: bool,
    has_q2: bool,
    fund_adjacent: bool,
    partner_led_in_target: bool,
    market_window_match: bool,
    company_traction_proof: bool,
) -> dict[str, int]:
    """0-3 score per strategy. Only >=2 may be used.

    `company_traction_proof` is now caller-computed as
    has_company_traction(...) AND has_metrics_oriented_signal(...). It must
    NOT be hardcoded True per finding #11.
    """
    return {
        "signal_led": 3 if has_q3 else (2 if has_q2 else 0),
        "portfolio_led": 3 if fund_adjacent else 0,
        "round_pattern_led": 3 if partner_led_in_target else 0,
        "market_shift_led": 2 if market_window_match else 0,
        "contrarian_thesis_led": 2 if has_q3 else 0,
        "traction_led": 2 if company_traction_proof else 0,
    }


# Tie-break order when two strategies score equally: strongest evidence shape
# first. signal_led/portfolio_led/round_pattern_led are concrete; market_shift
# and traction lean general; contrarian_thesis_led is last because it leans on
# rhetorical risk.
STRATEGY_TIE_BREAK = (
    "signal_led",
    "portfolio_led",
    "round_pattern_led",
    "market_shift_led",
    "traction_led",
    "contrarian_thesis_led",
)


def pick_strategies(elig: dict[str, int]) -> list[str]:
    """Return up to two eligible strategies, highest score first then tie-break."""
    eligible = sorted(
        [(s, score) for s, score in elig.items() if score >= 2],
        key=lambda x: (-x[1], STRATEGY_TIE_BREAK.index(x[0])),
    )
    return [s for s, _ in eligible[:2]]


# ------- stub email bank (fixture path) -------

DECK_RESPONSE_TEMPLATE = (
    "Deck attached. The reporting regimes that matter most depend on which "
    "fintechs in your portfolio sit closest to the mandates, so 30 minutes "
    "lets me show the slides that are actually relevant rather than the "
    "generic version. Happy to do that this week: https://cal.example/dana-tendril"
)

EMAIL_BANK: dict[str, dict] = {
    "northbeam.example_priya_anand": {
        "signal_led": {
            "subject": "Reporting wedge, productized",
            "body": (
                "On the Distribution podcast you said compliance reporting is the "
                "wedge nobody wants to build but everyone regulated has to buy. "
                "Tendril is that wedge productized: a regulatory-reporting API at "
                "$180K ARR with 128% net revenue retention across four design "
                "partners. "
                "State mandates land this quarter, so the wedge framing is about "
                "to be tested in production for every regulated fintech. "
                "I would like 30 minutes to walk through it; we are raising a $3M "
                "Seed closing in 8 weeks: https://cal.example/dana-tendril"
            ),
            "conversion_hypothesis": (
                "Anand's public wedge framing applies directly to a company that "
                "already monetizes that wedge, so the meeting is about underwriting "
                "rather than introducing a category."
            ),
            "likely_objection": "Too early; only four design partners.",
            "objection_preempted": True,
            "preemption_line": (
                "$180K ARR with 128% net revenue retention across four design partners"
            ),
        },
        "portfolio_led": {
            "subject": "Tendril, adjacent to Comply.io",
            "body": (
                "Comply.io and LedgerKit, both Northbeam-backed, sit one layer down "
                "the stack from where Tendril operates. "
                "We ship regulatory reporting as an API and have moved four design "
                "partners from build to buy with retention of 128% in year one. "
                "Mandates landing this quarter are pulling fintech pipelines ahead "
                "of plan, including ours. "
                "Raising a $3M Seed; first close 8 weeks out. I would like 30 "
                "minutes for the walk-through: https://cal.example/dana-tendril"
            ),
        },
        "followup_draft": (
            "Following up: we signed a fifth design partner this week and our "
            "first close is now 6 weeks out. Still worth 30 minutes to walk you "
            "through the round?"
        ),
    },
    "northbeam.example_marcus_lindqvist": {
        "signal_led": {
            "subject": "Reconciliation, plus reporting",
            "body": (
                "Your DevFin point that reconciliation infrastructure is the most "
                "boring and most valuable thing in fintech tracks the shape of "
                "what we are building. "
                "Tendril is the reporting-API layer for regulated fintechs, "
                "deployed in days rather than the months they typically lose to "
                "compliance wiring. "
                "Customers move from build to buy when a mandate landing date is "
                "fixed, which is exactly what is happening to our pipeline this "
                "quarter. "
                "Raising a $3M Seed; first close 8 weeks out. 30 minutes for the "
                "round: https://cal.example/dana-tendril"
            ),
            "conversion_hypothesis": (
                "Lindqvist values boring infrastructure-as-product; Tendril's "
                "reporting API is the same shape of plumbing his thesis underwrites."
            ),
            "likely_objection": "Reporting is a feature, not a company.",
            "objection_preempted": True,
            "preemption_line": (
                "deployed in days rather than the months they typically lose to "
                "compliance wiring"
            ),
        },
        "portfolio_led": {
            "subject": "Tendril, next to Paywall",
            "body": (
                "Paywall and Comply.io live in adjacent regulated-finance plumbing; "
                "Tendril extends that surface into reporting. "
                "$180K ARR, 128% NRR, four design partners shipping reporting via "
                "our API rather than internal builds. "
                "The mandate window this quarter is forcing fintechs to buy this "
                "category, ahead of our pipeline plan. "
                "We are raising a $3M Seed and I would like 30 minutes to walk "
                "you through the round: https://cal.example/dana-tendril"
            ),
        },
        "followup_draft": (
            "Follow-up: a regulated payments customer just signed a six-figure "
            "pilot and first close is 6 weeks out. Worth 30 minutes to walk you "
            "through the round?"
        ),
    },
    "tidewater.example_dana_cole": {
        "signal_led": {
            "subject": "Design partners, not sign-ups",
            "body": (
                "Your Tidewater note that five design partners doing real work "
                "beats a thousand sign-ups is the exact motion that took Tendril "
                "to $180K ARR. "
                "We have four paying design partners on our regulatory reporting "
                "API, retention at 128% in a category where churn is normally high. "
                "State mandates this quarter are turning every regulated fintech "
                "into a forced buyer of this category rather than a builder. "
                "We are raising a $3M Seed closing in 8 weeks and I would like 30 "
                "minutes to walk through the round: https://cal.example/dana-tendril"
            ),
            "conversion_hypothesis": (
                "Cole's investment criterion (design partners over PLG) is exactly "
                "how Tendril got to its first revenue, so the conversation maps to "
                "her stated decision frame."
            ),
            "likely_objection": "Wrong stack; Tendril is fintech, not pure B2B ops.",
            "objection_preempted": True,
            "preemption_line": (
                "turning every regulated fintech into a forced buyer of this category "
                "rather than a builder"
            ),
        },
        "traction_led": {
            "subject": "Tendril seed, 128% NRR",
            "body": (
                "Tendril is at $180K ARR with 128% net revenue retention across "
                "four design partners in a category where churn is normally high. "
                "The product is a regulatory-reporting API for fintechs, replacing "
                "months of internal compliance work with days of integration. "
                "Mandate landing dates this quarter mean buyers no longer have the "
                "luxury of building it themselves. "
                "We are raising a $3M Seed; I would like 30 minutes for the "
                "walk-through: https://cal.example/dana-tendril"
            ),
        },
        "followup_draft": (
            "Follow-up: our pipeline added two more fintechs this week and we are "
            "6 weeks from first close. Still worth 30 minutes to walk you through "
            "the round?"
        ),
    },
    "tidewater.example_renee_park": {
        "signal_led": {
            "subject": "Founder sales, regulated buyers",
            "body": (
                "Your substack point that a seed founder not doing sales themselves "
                "does not understand the buyer is how I closed our first four "
                "design partners on the regulatory reporting API. "
                "Tendril is at $180K ARR, retention 128%, in a category where "
                "buyers are about to be forced into the buy decision by mandate "
                "deadlines. "
                "Founder-led sales is also the only way I have been able to read "
                "the compliance reporting buyer before the category was obvious. "
                "Raising a $3M Seed closing in 8 weeks; 30 minutes for the round: "
                "https://cal.example/dana-tendril"
            ),
            "conversion_hypothesis": (
                "Park's belief about founder-led sales maps to the literal motion "
                "that produced Tendril's first four design partners."
            ),
            "likely_objection": "Stage and check-size mismatch with Tidewater.",
            "objection_preempted": False,
            "preemption_line": None,
        },
        "traction_led": {
            "subject": "Tendril, retention at 128%",
            "body": (
                "Four paying design partners on Tendril's regulatory reporting API, "
                "$180K ARR, retention 128% in a category where churn is normally "
                "the dominant force. "
                "The product replaces months of compliance wiring with days of "
                "integration via API, which is what makes the retention hold. "
                "State mandate dates land this quarter; the buyers in our pipeline "
                "are turning from build into buy faster than we are raising. "
                "Raising a $3M Seed and I would like 30 minutes on the round: "
                "https://cal.example/dana-tendril"
            ),
        },
        "followup_draft": (
            "Follow-up: design partner number five just signed and first close is "
            "6 weeks out. Worth 30 minutes for the walk-through?"
        ),
    },
    "foundrynorth.example_kwame_boateng": {
        "signal_led": {
            "subject": "Policy windows, regulated fintech",
            "body": (
                "Your Climate Podcast framing that policy-shaped markets create "
                "forced-buy windows worth underwriting is the exact dynamic "
                "playing out in fintech compliance this quarter. "
                "Tendril is the regulatory-reporting API turning that mandate "
                "window into a buy decision for fintechs; $180K ARR, retention "
                "128%, four design partners. "
                "Pipeline is pulling ahead of the round because the buyers no "
                "longer have the option of waiting. "
                "Raising a $3M Seed; I would like 30 minutes to walk you through "
                "the round: https://cal.example/dana-tendril"
            ),
            "conversion_hypothesis": (
                "Boateng's policy-driven forced-buy framing is exactly the playbook "
                "Tendril is running in regulated finance, even if his usual sector "
                "is climate."
            ),
            "likely_objection": "Wrong sector; Foundry is climate-focused.",
            "objection_preempted": True,
            "preemption_line": (
                "the exact dynamic playing out in fintech compliance this quarter"
            ),
        },
        "market_shift_led": {
            "subject": "Mandate window, buy or fail",
            "body": (
                "New state-level reporting mandates landing this quarter are "
                "turning regulated fintech compliance into a buy-or-fail decision, "
                "the same policy-window shape Foundry underwrites in climate. "
                "Tendril ships the reporting layer as an API; $180K ARR with "
                "retention 128% across our first four design partners. "
                "The fintechs facing these mandates are pulling our pipeline "
                "forward faster than the round itself. "
                "We are raising a $3M Seed and I would like 30 minutes for the "
                "walk-through: https://cal.example/dana-tendril"
            ),
        },
        "followup_draft": (
            "Follow-up: another regulated fintech signed a pilot this week and "
            "first close is 6 weeks out. 30 minutes to walk you through the round?"
        ),
    },
}


# ------- live LLM prompt assembly (built but exercised only when key present) -------

def _read_example_files(examples_dir) -> str:
    """Concatenate every prompts/examples/*.md into one block for the live
    prompt. The base prompt previously said 'load the corresponding example
    file as a style anchor', but the LLM has no filesystem access -- it was
    being told to use anchors that were never sent. Each file is wrapped in
    a header so the model can see which strategy it belongs to."""
    from pathlib import Path
    examples_dir = Path(examples_dir)
    if not examples_dir.exists():
        return "(no example files available)"
    chunks: list[str] = []
    for path in sorted(examples_dir.glob("*.md")):
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            continue
        chunks.append(f"--- {path.stem} ---\n{body}")
    return "\n\n".join(chunks) if chunks else "(no example files available)"


def build_live_prompt(*, company_cfg, partner_name, fund_name, partner_bio,
                      composite_score, round_fit_score, round_fit_reasoning,
                      lead_likelihood_score, axes_summary, fund_kill_signals,
                      signals_for_partner, deals_for_partner,
                      examples_dir) -> str:
    c = company_cfg["company"]
    rc = company_cfg["raise_context"]
    rh = rc.get("round_hook") or {}
    return (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{COMPANY_NAME}", c["name"])
        .replace("{FOUNDER_NAME}", c["founder_name"])
        .replace("{ROUND}", rc.get("round", ""))
        .replace("{RAISE_AMOUNT}", rc.get("amount", ""))
        .replace("{RAISE_STATUS}", rc.get("status", ""))
        .replace("{RAISE_TIMING}", rc.get("timing", ""))
        .replace("{WHY_THIS_ROUND_IS_FUNDABLE_NOW}", rc.get("why_this_round_is_fundable_now", ""))
        .replace("{WHAT_CHANGES_AFTER_THIS_ROUND}", rc.get("what_changes_after_this_round", ""))
        .replace("{ROUND_HOOK_REASON}", rh.get("strongest_reason_to_meet_now", ""))
        .replace("{ROUND_HOOK_CONSEQUENCE}", rh.get("investor_consequence_of_waiting", ""))
        .replace("{ROUND_HOOK_MOMENTUM_PROOF}", rh.get("round_momentum_proof", ""))
        .replace("{COMPANY_DESCRIPTION}", c.get("description", ""))
        .replace("{STRONGEST_RAISE_PROOF}", rc.get("strongest_raise_proof", ""))
        .replace("{HEADLINE_METRIC}", c.get("current_traction", {}).get("headline_metric", ""))
        .replace("{SECONDARY_METRICS}", ", ".join(c.get("current_traction", {}).get("secondary_metrics", [])))
        .replace("{CUSTOMER_EVIDENCE}", "")
        .replace("{TECHNICAL_VALIDATION}", "")
        .replace("{NON_DILUTIVE_OR_STRATEGIC}", rc.get("notable_existing_investors_or_non_dilutive", ""))
        .replace("{FOUNDER_MARKET_FIT}", "")
        .replace("{PARTNER_NAME}", partner_name or "")
        .replace("{FUND_NAME}", fund_name or "")
        .replace("{PARTNER_BIO}", partner_bio or "")
        # Finding 5: stop sending blank scoring context to the live LLM.
        .replace("{COMPOSITE_SCORE}",
                 "" if composite_score is None else f"{composite_score:.2f}")
        .replace("{ROUND_FIT_SCORE}",
                 "" if round_fit_score is None else f"{round_fit_score:.1f}")
        .replace("{LEAD_LIKELIHOOD_SCORE}",
                 "" if lead_likelihood_score is None else f"{lead_likelihood_score:.1f}")
        .replace("{TOP_AXES_NAMES_AND_SCORES}", axes_summary or "")
        .replace("{TOP_SIGNALS}", json.dumps([
            {"quote": s["quote"], "url": s["source_url"], "date": str(s.get("date"))}
            for s in signals_for_partner[:3]
        ]))
        # Stage 2 does not yet persist per-fund portfolio_companies; left
        # blank with a comment so the operator knows it's a known gap.
        .replace("{ADJACENT_PORTFOLIO_COMPANIES}", "")
        .replace("{RECENT_PARTNER_LED_DEALS}", json.dumps([
            {"company": d["company"], "round": d.get("round_type")}
            for d in deals_for_partner
        ]))
        # COMM_STYLE would need linguistic analysis we don't yet do.
        .replace("{COMM_STYLE}", "")
        .replace("{KILL_SIGNALS}", fund_kill_signals or "")
        .replace("{FOUNDER_VOICE_STYLE}", (company_cfg.get("founder_voice") or {}).get("style", ""))
        .replace("{FOUNDER_BANNED_PHRASES}", ", ".join(
            (company_cfg.get("founder_voice") or {}).get("banned_phrases", [])
        ))
        # Inject the actual file contents AND keep the legacy {EXAMPLES_DIR}
        # token for backward-compatibility with any custom prompts that still
        # reference the directory path.
        .replace("{EXAMPLES_BLOCK}", _read_example_files(examples_dir))
        .replace("{EXAMPLES_DIR}", str(examples_dir))
        .replace("{MEETING_DURATION}", str(c.get("meeting_ask", {}).get("duration_minutes", 30)))
        .replace("{MEETING_FORMAT}", c.get("meeting_ask", {}).get("format", "video call"))
        .replace("{SCHEDULING_LINK}", c.get("meeting_ask", {}).get("preferred_scheduling_link", ""))
        # Finding 6: {TIME_1}/{TIME_2} were never substituted; a live LLM
        # could emit literal placeholders. Pull from
        # company.meeting_ask.preferred_time_slots if set; else fill with a
        # neutral string. check_hard_gates ALSO rejects any leftover
        # `{...}` placeholder in the body as a belt-and-suspenders guard.
        .replace("{TIME_1}", _meeting_slot(c, 0))
        .replace("{TIME_2}", _meeting_slot(c, 1))
    )


def _meeting_slot(company_block: dict, idx: int) -> str:
    slots = (company_block.get("meeting_ask") or {}).get("preferred_time_slots") or []
    if idx < len(slots) and slots[idx]:
        return str(slots[idx])
    # Sentinel that won't slip past the placeholder hard gate if the LLM
    # decides to use the slots-only CTA when slots aren't configured.
    return "(no time slot configured)"


def build_stub_response(partner_id: str, strategies: list[str]) -> dict | None:
    """Construct an EmailOutput-shaped stub from the in-script bank.

    Returns None if the partner has no bank entry (stub mode can't generate).
    """
    bank = EMAIL_BANK.get(partner_id)
    if not bank:
        return None
    variants = []
    for s in strategies:
        if s not in bank:
            continue
        v = bank[s]
        variants.append({
            "strategy": s,
            "subject": v["subject"],
            "body": v["body"],
            "conversion_hypothesis": v.get("conversion_hypothesis", ""),
            "likely_objection": v.get("likely_objection", ""),
            "objection_preempted": v.get("objection_preempted", False),
            "preemption_line": v.get("preemption_line"),
            "template_smell": "unscored",
        })
    if not variants:
        return None
    limited = len(variants) < 2
    return {
        "variants": variants,
        "recommended_variant_strategy": variants[0]["strategy"],
        "recommendation_reasoning": (
            "Primary strategy carries the strongest evidence; alternate offers a "
            "different opening logic at acceptable eligibility."
        ),
        "limited_variation": limited,
        "limited_variation_reason": (
            "only one eligible strategy in fixture bank" if limited else None
        ),
        "deck_request_response": DECK_RESPONSE_TEMPLATE,
        "followup_draft": bank.get("followup_draft", ""),
    }


# ------- batch QA -------

_RAISE_RE = re.compile(r"\b(raising|raise|seed round|series [a-z])\b", re.IGNORECASE)


def check_hard_gates(draft: dict, banned: list[str]) -> list[str]:
    """Per-draft hard gates that disqualify a draft."""
    fails: list[str] = []
    body = draft.get("body") or ""
    body_lower = body.lower()
    # Finding 6: refuse literal `{X}` placeholders the model might have
    # emitted (TIME_1/TIME_2 are the obvious ones, but the gate catches
    # any uppercase-token placeholder so future prompt changes can't slip).
    leftover = re.findall(r"\{[A-Z][A-Z0-9_]*\}", body)
    if leftover:
        fails.append(
            f"unfilled prompt placeholder(s) in body: {sorted(set(leftover))}"
        )
    # Word-boundary match so "$3M raise." and "Seed round closing" both count.
    # Previous substring match required " raise " with surrounding spaces and
    # missed sentence-final forms.
    if not _RAISE_RE.search(body):
        fails.append("missing explicit raise reference in body")
    if any(p in body_lower for p in SOFT_CTA_PHRASES):
        fails.append("soft CTA phrase present")
    for ph in UNIVERSAL_FORBIDDEN + banned:
        if ph and ph.lower() in body_lower:
            fails.append(f"forbidden phrase: {ph!r}")
    if "—" in (draft.get("body") or ""):
        fails.append("em dash in body")
    if "!" in (draft.get("body") or ""):
        fails.append("exclamation mark in body")
    return fails


def template_smell_judge(
    draft_body: str, neighbor_bodies: list[str]
) -> tuple[str, bool, bool]:
    """Heuristic stub judge: returns (smell, sounds_mass_generated, too_similar).

    `high` is reserved for near-duplicates above the body hard gate (0.82). The
    judge promotes to `medium` when a draft shares its opening structure with a
    neighbor (the brief's "same first-sentence structural pattern" warning) or
    sits in the 0.80-0.82 body-similarity band. Token-set similarity in a tight
    single-company batch will inherently run in the 0.60-0.78 range due to
    shared CTA and product vocabulary; that range is `low`.
    """
    if not neighbor_bodies:
        return "low", False, False
    body_sims = [token_set_similarity(draft_body, n) for n in neighbor_bodies if n]
    fs_a = first_sentence(draft_body)
    fs_sims = [
        ratio_similarity(fs_a, first_sentence(n)) for n in neighbor_bodies if n
    ]
    max_body = max(body_sims) if body_sims else 0.0
    max_first = max(fs_sims) if fs_sims else 0.0
    too_similar = max_body > 0.78
    mass = max_first > 0.70
    if max_body > 0.82:
        return "high", mass, True
    if mass or max_body > 0.80:
        return "medium", mass, too_similar
    return "low", mass, too_similar


def evaluate_batch(
    recommended_drafts: list[dict],
    all_drafts: list[dict],
) -> dict:
    """Compute similarity, template_smell, and gate report for the batch."""
    # Similarity check across recommended drafts.
    sim_failures: list[tuple[str, str, str, float]] = []
    bodies = [(d["partner_id"], d["body"]) for d in recommended_drafts]
    subjects = [(d["partner_id"], d.get("subject") or "") for d in recommended_drafts]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            sb = token_set_similarity(bodies[i][1], bodies[j][1])
            if sb > SIM_BODY_HARD:
                sim_failures.append((bodies[i][0], bodies[j][0], "body", sb))
            fa = first_sentence(bodies[i][1])
            fb = first_sentence(bodies[j][1])
            sf = ratio_similarity(fa, fb)
            if sf > SIM_FIRST_HARD:
                sim_failures.append((bodies[i][0], bodies[j][0], "first_sentence", sf))
            ss = ratio_similarity(subjects[i][1], subjects[j][1])
            if ss > SIM_SUBJECT_HARD:
                sim_failures.append((subjects[i][0], subjects[j][0], "subject", ss))

    # Per-draft template-smell judging against 5 nearest neighbors.
    for d in all_drafts:
        others = [o["body"] for o in all_drafts if o is not d]
        others_with_sim = sorted(
            ((token_set_similarity(d["body"], b), b) for b in others),
            key=lambda x: -x[0],
        )[:5]
        neighbors = [b for _, b in others_with_sim]
        smell, mass, too_sim = template_smell_judge(d["body"], neighbors)
        d["template_smell"] = smell
        d["sounds_mass_generated"] = mass
        d["too_similar_to_neighbors"] = too_sim

    smell_high_count = sum(1 for d in all_drafts if d["template_smell"] == "high")
    smell_low_count = sum(1 for d in all_drafts if d["template_smell"] == "low")
    raise_missing = sum(
        1 for d in all_drafts if not _RAISE_RE.search(d.get("body") or "")
    )

    # Strategy distribution (recommended drafts only).
    strategy_counts = Counter(d["strategy"] for d in recommended_drafts)
    n_rec = max(1, len(recommended_drafts))
    warnings: list[str] = []
    for strat, n in strategy_counts.items():
        if n / n_rec > WARN_STRATEGY_SHARE:
            warnings.append(
                f"strategy {strat!r} used by {n}/{n_rec} drafts "
                f"({n/n_rec:.0%}); evidence quality should justify it"
            )
    smell_low_share = smell_low_count / max(1, len(all_drafts))
    if smell_low_share < WARN_TEMPLATE_LOW_SHARE:
        warnings.append(
            f"only {smell_low_share:.0%} of drafts are template_smell=low "
            f"(target >= {WARN_TEMPLATE_LOW_SHARE:.0%})"
        )

    hard_fail_reasons: list[str] = []
    if sim_failures:
        hard_fail_reasons.append(f"{len(sim_failures)} similarity gate failure(s)")
    if smell_high_count:
        hard_fail_reasons.append(f"{smell_high_count} draft(s) template_smell=high")
    if raise_missing:
        hard_fail_reasons.append(f"{raise_missing} draft(s) missing raise reference")

    return {
        "similarity_failures": sim_failures,
        "similarity_failure_count": len(sim_failures),
        "template_smell_high_count": smell_high_count,
        "template_smell_low_count": smell_low_count,
        "raise_reference_missing_count": raise_missing,
        "strategy_distribution": dict(strategy_counts),
        "warnings": warnings,
        "hard_fail_reasons": hard_fail_reasons,
        "passed": not hard_fail_reasons,
    }


# ------- main -------

def _now() -> datetime:
    return datetime.now(timezone.utc)


READY_TO_SEND_DAILY_CEILING = 25
# Brief Gate 5.5: before scaling beyond mid-priority into top-25, a Green
# calibration cohort must exist within the last 60 days. --skip-calibration
# --reason "..." overrides for calibration runs themselves and emergencies.
TOP_BEFORE_CALIBRATION_REQUIRED = 10
CALIBRATION_WINDOW_DAYS = 60

# Batch 17 (#363/#364/#365): refuse Stage 7 when the dependency stages
# are stale relative to each other. STALE_STAGE6_HOURS bounds Stage 6
# freshness; STAGE_DEPENDENCIES enforces "Stage Y must run AFTER Stage X"
# so a re-run of Stage 4 followed directly by Stage 7 (without re-running
# Stage 5 verification + Stage 6 scoring) gets refused.
STALE_STAGE6_HOURS = 24
STAGE_DEPENDENCIES = (
    # (downstream, upstream) -- downstream must have completed after upstream
    ("05_verify_and_quality", "04_mine_partner_signals"),
    ("06_score_candidates", "05_verify_and_quality"),
    ("06_score_candidates", "03_mine_activity"),
)


def _check_stage_freshness(engine) -> list[str]:
    """Return human-readable reasons Stage 7 should refuse to run.

    Empty list means upstream stages are in a consistent, recent state.
    Each reason names the specific stage problem so the operator can
    re-run the right script.
    """
    from datetime import timedelta as _td
    from core.db import runs as _runs
    from sqlalchemy import desc as _desc, select as _select
    problems: list[str] = []

    def _latest_completed(stage: str):
        with engine.begin() as conn:
            row = conn.execute(
                _select(_runs.c.completed_at, _runs.c.records_failed)
                .where(_runs.c.stage == stage,
                       _runs.c.completed_at.isnot(None))
                .order_by(_desc(_runs.c.run_id)).limit(1)
            ).first()
        return row

    s6 = _latest_completed("06_score_candidates")
    if s6 is None:
        problems.append("Stage 6 has never completed")
    else:
        if (s6.records_failed or 0) > 0:
            problems.append(
                f"Stage 6 last run had records_failed={s6.records_failed}; "
                f"fix and re-run scripts/06_score_candidates.py first"
            )
        # SQLite stores naive datetimes; compare both sides naively.
        age_hours = (
            datetime.now(timezone.utc).replace(tzinfo=None) - s6.completed_at
        ).total_seconds() / 3600.0
        if age_hours > STALE_STAGE6_HOURS:
            problems.append(
                f"Stage 6 last completed {age_hours:.1f}h ago "
                f"(threshold {STALE_STAGE6_HOURS}h); re-run "
                f"scripts/06_score_candidates.py"
            )

    for downstream, upstream in STAGE_DEPENDENCIES:
        d = _latest_completed(downstream)
        u = _latest_completed(upstream)
        if d is None or u is None:
            # Already covered by "never completed" check above (or upstream
            # not yet run, which Stage 7 would surface via empty results).
            continue
        if d.completed_at < u.completed_at:
            problems.append(
                f"{downstream} (last completed {d.completed_at}) is "
                f"OLDER than its upstream {upstream} "
                f"(last completed {u.completed_at}); re-run "
                f"scripts/{downstream}.py"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 email generation + CSV write.")
    add_workspace_arg(parser)
    parser.add_argument("--top", type=int, default=25,
                        help="Top-N partners by send_now_priority (Gate 5 uses 5).")
    parser.add_argument(
        "--approve-bulk-ready", action="store_true",
        help="Required to mark more than 25 partners as ready_to_send in a "
             "single run (Brief Rule 16 hard ceiling). Requires --reason.",
    )
    parser.add_argument(
        "--skip-calibration", action="store_true",
        help="Bypass the Gate 5.5 calibration check (you scaled before having "
             "a Green calibration in the last 60 days). Requires --reason.",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Required with --approve-bulk-ready or --skip-calibration.",
    )
    parser.add_argument(
        "--allow-example-domains", action="store_true",
        help="Permit RFC 2606 reserved domains (.example/.test/.invalid) "
             "in scheduling links, founder email, and partner email when "
             "deciding ready_to_send. Use for fixture / smoke-test runs "
             "ONLY; production workspaces should configure real domains.",
    )
    # Batch 17 #363/#364/#365.
    parser.add_argument(
        "--skip-freshness-check", action="store_true",
        help="Skip the stage-freshness preflight (Batch 17). Use only "
             "when you knowingly want to regenerate emails against a "
             "stale Stage 6. Requires --reason.",
    )
    args = parser.parse_args()
    if (args.approve_bulk_ready or args.skip_calibration
        or args.skip_freshness_check) and not args.reason:
        parser.error(
            "--approve-bulk-ready / --skip-calibration / "
            "--skip-freshness-check require --reason \"...\""
        )

    # Refactor sweep: stage_run() boilerplate collapse. Examples anchor
    # the LIVE prompt; stub mode skips the LLM so we only require
    # example files when ANTHROPIC_API_KEY is resolvable. Workspace .env
    # may carry the key, so peek the workspace before stage_run() decides
    # which preflight checks to run.
    from core.config_loader import load_workspace as _peek_ws
    _require_examples = bool(_peek_ws(args.workspace).env("ANTHROPIC_API_KEY"))
    with stage_run(args, stage=STAGE, require_examples=_require_examples) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm
        # WorkspacePolicy centralizes mode-driven defaults (item 10).
        from core.workspace_policy import WorkspacePolicy
        policy = WorkspacePolicy.from_workspace_and_args(ws, args)
        batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        banned = (ws.company.get("founder_voice") or {}).get("banned_phrases", []) or []
        target_sectors = {
            s.lower()
            for s in (ws.company.get("company") or {}).get("target_sectors", []) or []
        }
        market_shift_axes = market_shift_axis_ids(ws.axes)

        # ---- pull top-N partners + their context ----
        # Stage 7 generates outreach drafts; it should only operate on
        # partners that Stage 6 said are recommended_to_send. Previously
        # the query selected top-N by send_now_priority with NO
        # recommendation filter, so a partner who failed Stage 6's
        # criteria 1-9 could still get a draft generated (and consume
        # one of the --top slots). rec_in_batch only saw the filtered
        # set for the Rule 16 ceiling gate but the loop saw everyone.
        with engine.begin() as conn:
            rows = list(conn.execute(
                select(
                    partner_score_summaries,
                    partners.c.name.label("partner_name"),
                    partners.c.title,
                    partners.c.linkedin_url,
                    partners.c.warm_path_available,
                    partners.c.bio,
                    funds.c.name.label("fund_name"),
                    funds.c.domain.label("fund_domain"),
                    funds.c.stated_thesis,
                    # Finding 5: surface fund-level kill signals into the live
                    # prompt so the LLM can avoid triggering them.
                    funds.c.kill_signals.label("fund_kill_signals"),
                )
                .join(partners, partners.c.partner_id == partner_score_summaries.c.partner_id)
                .join(funds, funds.c.fund_id == partners.c.fund_id)
                .where(partner_score_summaries.c.recommended_to_send.is_(True))
                .order_by(partner_score_summaries.c.send_now_priority.desc())
                .limit(args.top)
            ))
            # Per-partner per-axis scores -- consumed by the live prompt's
            # TOP_AXES_NAMES_AND_SCORES placeholder so the LLM strategy picker
            # has the deterministic axis ranking in hand.
            from core.db import scores as _scores
            axis_scores_by_partner: dict[str, list[tuple[str, float | None]]] = {}
            for s in conn.execute(select(_scores)):
                axis_scores_by_partner.setdefault(s.partner_id, []).append(
                    (s.axis_id, s.score)
                )
            # Per-partner: verified quality>=2 signals.
            signals_by_partner: dict[str, list[dict]] = {}
            for s in conn.execute(
                select(signals).where(
                    signals.c.verified.is_(True),
                    signals.c.signal_quality_score >= 2,
                )
            ):
                signals_by_partner.setdefault(s.partner_id, []).append({
                    "id": int(s.signal_id),
                    "quote": s.quoted_text,
                    "source_url": s.source_url,
                    "source_type": s.source_type,
                    "axes": json.loads(s.axis_relevance or "[]"),
                    "direction": s.signal_direction,
                    "quality": int(s.signal_quality_score),
                    "date": s.quote_date,
                })
            # Partner-attributed deals.
            deals_by_partner: dict[str, list[dict]] = {}
            for d in conn.execute(select(deal_attributions)):
                if d.attributed_partner_id:
                    deals_by_partner.setdefault(d.attributed_partner_id, []).append({
                        "company": d.company,
                        "round_type": d.round_type,
                        "round_size_usd": d.round_size_usd,
                        "announcement_date": d.announcement_date,
                        "lead_fund_id": d.lead_fund_id,
                    })

        # Rows are already filtered to recommended_to_send=True by the
        # query above; this list is kept for the ceiling gate's intent
        # (count of partners that would land as ready_to_send).
        rec_in_batch = rows

        # Both safety refusals (Gate 5.5 calibration + Rule 16 ceiling) now
        # live inside the RunLogger context so the refusal lands in `runs`
        # with run.failed=1 and an audit note. The previous early-returns
        # produced no run row -- the most important refusals were invisible
        # to status.py / audit.

        # Batch 17 (#363/#364/#365): stage-freshness preflight.
        if not args.skip_freshness_check:
            fresh_problems = _check_stage_freshness(engine)
            if fresh_problems:
                msg = (
                    "FRESHNESS REFUSED: "
                    + "; ".join(fresh_problems)
                    + " (re-run the upstream stage, OR pass "
                      "--skip-freshness-check --reason '...')"
                )
                print(f"[stage 7] {msg}")
                run.note(msg)
                run.failed = max(run.failed, 1)
                return 2
        elif args.skip_freshness_check:
            run.note(
                f"FRESHNESS_SKIPPED reason={args.reason!r}"
            )

        # Gate 5.5: scaling beyond mid-tier without a recent Green cal.
        if args.top > TOP_BEFORE_CALIBRATION_REQUIRED and not args.skip_calibration:
            from datetime import timedelta as _td
            from core.db import calibration_cohorts as _cc
            from sqlalchemy import select as _select, desc as _desc
            cutoff = datetime.now(timezone.utc) - _td(days=CALIBRATION_WINDOW_DAYS)
            with engine.begin() as conn:
                green = conn.execute(
                    _select(_cc).where(
                        _cc.c.outcome == "green",
                        _cc.c.completed_at >= cutoff,
                    ).order_by(_desc(_cc.c.completed_at)).limit(1)
                ).first()
            if not green:
                msg = (
                    f"GATE 5.5 REFUSED: --top={args.top} > "
                    f"{TOP_BEFORE_CALIBRATION_REQUIRED} requires a Green "
                    f"calibration cohort within the last "
                    f"{CALIBRATION_WINDOW_DAYS} days; none found. Run "
                    f"scripts/calibration.py --start, or pass "
                    f"--skip-calibration --reason \"...\"."
                )
                print(f"[stage 7] {msg}")
                run.note(msg)
                run.failed = 1
                return 2

        # Rule 16 hard ceiling.
        if (
            len(rec_in_batch) > READY_TO_SEND_DAILY_CEILING
            and not args.approve_bulk_ready
        ):
            msg = (
                f"HARD CEILING REFUSED: {len(rec_in_batch)} partners would "
                f"be marked ready_to_send (> {READY_TO_SEND_DAILY_CEILING}). "
                f"Re-run with --approve-bulk-ready --reason \"...\"."
            )
            print(f"[stage 7] {msg}")
            run.note(msg)
            run.failed = 1
            return 2
        if args.approve_bulk_ready:
            # Log the approval into the runs row's audit summary (Criterion 15).
            run.note(
                f"BULK_READY_APPROVED count={len(rec_in_batch)} "
                f"reason={args.reason!r}"
            )
            print(
                f"[stage 7] bulk-ready approved by user: "
                f"{len(rec_in_batch)} records / reason={args.reason!r}"
            )
        if args.skip_calibration:
            run.note(f"CALIBRATION_SKIPPED reason={args.reason!r}")
            print(
                f"[stage 7] calibration check skipped by user: "
                f"reason={args.reason!r}"
            )
        recommended_drafts: list[dict] = []
        all_drafts: list[dict] = []
        partner_outputs: list[tuple[dict, EmailOutput, list[str]]] = []

        for row in rows:
            with run.attempt():
                partner_id = row.partner_id
                try:
                    p_signals = signals_by_partner.get(partner_id, [])
                    p_deals = deals_by_partner.get(partner_id, [])

                    # ---- strategy eligibility ----
                    # signal_led / contrarian_thesis_led need a partner quote
                    # the email can riff on positively. A negative-direction
                    # quote ("regulation kills startups") at quality=3 does NOT
                    # unlock signal_led -- it's evidence of MISFIT, not signal.
                    positive_signals = [
                        s for s in p_signals
                        if (s.get("direction") or "").lower() == "positive"
                    ]
                    has_q3 = any(s["quality"] >= 3 for s in positive_signals)
                    has_q2 = any(s["quality"] >= 2 for s in positive_signals)
                    # Loose single-keyword match is too generous (e.g. "infrastructure"
                    # matches both Foundry-style climate-infra and Northbeam-style
                    # fintech-infra). Require >=2 target-sector keyword hits.
                    # Batch 24 (#420): substring matching causes "art" -> "smart"
                    # and "ai" -> "stairwell" false positives. Use word-
                    # boundary matching against tokenized thesis so multi-word
                    # sectors like "design partners" still hit correctly.
                    thesis_lower = (row.stated_thesis or "").lower()
                    fund_adjacent = sum(
                        1 for kw in target_sectors
                        if kw and _word_boundary_hit(thesis_lower, kw)
                    ) >= 2
                    # partner_led_in_target: partner has a named-lead deal at a fund
                    # whose thesis is target-adjacent.
                    partner_led_in_target = bool(p_deals) and fund_adjacent
                    # market_shift_led eligibility: partner has POSITIVE-direction
                    # signal tagged with an axis whose name/description signals
                    # timing-driven category conviction (resolved from axes.yaml;
                    # previously hardcoded to axis_4 and ignored direction).
                    market_window_match = bool(market_shift_axes) and any(
                        set(s.get("axes") or []) & market_shift_axes
                        for s in positive_signals
                    )
                    # Finding 11: traction_led requires BOTH the company having
                    # current traction in config AND THIS partner having a
                    # metrics-oriented signal in their quoted_text. Previously
                    # this was hardcoded True, which would have flagged
                    # traction_led for partners with no metric vocabulary in
                    # their public signal.
                    company_traction_proof = (
                        has_company_traction(ws.company)
                        and has_metrics_oriented_signal(p_signals)
                    )

                    elig = compute_eligibility(
                        has_q3=has_q3,
                        has_q2=has_q2,
                        fund_adjacent=fund_adjacent,
                        partner_led_in_target=partner_led_in_target,
                        market_window_match=market_window_match,
                        company_traction_proof=company_traction_proof,
                    )
                    strategies = pick_strategies(elig)
                    if not strategies:
                        run.skip()
                        run.log_error(
                            partner_id, "no_eligible_strategies",
                            f"strategy eligibility: {elig}"
                        )
                        continue

                    # ---- generate (live LLM or stub) ----
                    stub = build_stub_response(partner_id, strategies)
                    # Stub mode (no ANTHROPIC_API_KEY): EMAIL_BANK miss means we
                    # can't produce variants offline. WARN + skip (Brief Rule 14:
                    # no silent failures), count as skipped not succeeded.
                    # Live mode: stub being None is fine -- the LLM runs against
                    # prompts/generate_email.txt and stub_response is unused.
                    if stub is None and llm.stub:
                        print(
                            f"[stage 7] WARN: partner {partner_id} "
                            f"({row.partner_name}) has no entry in stub "
                            "EMAIL_BANK; no variants generated. A live LLM "
                            "would handle this partner via prompts/generate_email.txt."
                        )
                        run.skip()
                        run.log_error(
                            partner_id, "stub_bank_miss",
                            "stub mode: no EMAIL_BANK entry for this partner",
                        )
                        continue
                    # Live mode would build the full prompt; in stub mode the
                    # client validates the stub directly. Finding 5: surface
                    # composite/round_fit/lead_likelihood scores, per-axis
                    # summary, and fund kill_signals into the prompt so the
                    # live LLM strategy picker is properly grounded.
                    axes_for_p = sorted(
                        axis_scores_by_partner.get(partner_id, []),
                        key=lambda a: -(a[1] or 0),
                    )
                    axes_summary = ", ".join(
                        f"{ax_id} ({score:.1f})" for ax_id, score in axes_for_p
                        if score is not None
                    )
                    prompt = build_live_prompt(
                        company_cfg=ws.company,
                        partner_name=row.partner_name,
                        fund_name=row.fund_name,
                        partner_bio=getattr(row, "bio", None),
                        composite_score=getattr(row, "composite_fit_score", None),
                        round_fit_score=getattr(row, "round_fit_score", None),
                        round_fit_reasoning=getattr(row, "round_fit_reasoning", None),
                        lead_likelihood_score=getattr(row, "lead_likelihood_score", None),
                        axes_summary=axes_summary,
                        fund_kill_signals=getattr(row, "fund_kill_signals", None),
                        signals_for_partner=p_signals,
                        deals_for_partner=p_deals,
                        examples_dir=ws.examples_dir,
                    )
                    output: EmailOutput = llm.complete_json(
                        prompt=prompt,
                        schema=EmailOutput,
                        model=MODEL_EMAIL,
                        stub_response=stub,
                    )

                    # Track drafts for batch QA.
                    draft_recs: list[str] = []
                    for v in output.variants:
                        is_rec = (v.strategy == output.recommended_variant_strategy)
                        draft_recs.append(v.strategy)
                        rec = {
                            "partner_id": partner_id,
                            "partner_name": row.partner_name,
                            "strategy": v.strategy,
                            "subject": v.subject,
                            "body": v.body,
                            "is_recommended": is_rec,
                        }
                        all_drafts.append(rec)
                        if is_rec:
                            recommended_drafts.append(rec)
                    partner_outputs.append((dict(row._mapping), output, draft_recs))
                except Exception as exc:  # noqa: BLE001
                    run.fail(partner_id, type(exc).__name__, str(exc))

        # ---- batch QA ----
        qa = evaluate_batch(recommended_drafts, all_drafts)

        # ---- batch QA hard gate ----
        # If batch QA failed (similarity dupes, template_smell=high in any
        # draft, or missing raise references), refuse to publish. We still
        # record an audit row in batch_qa_reports so the operator can see
        # WHICH batch failed and WHY, but:
        #   - no new email_drafts/followup_drafts/deck_request_responses
        #     are inserted (prior good batch survives intact)
        #   - review_queue.csv is NOT overwritten (last good CSV stays)
        #   - run.failed=1 + the reasons land in runs.error_summary
        if not qa["passed"]:
            with engine.begin() as conn:
                conn.execute(batch_qa_reports.insert().values(
                    batch_id=batch_id,
                    batch_size=len(all_drafts),
                    strategy_distribution=json.dumps(qa["strategy_distribution"]),
                    similarity_failures=qa["similarity_failure_count"],
                    template_smell_high_count=qa["template_smell_high_count"],
                    raise_reference_missing_count=qa["raise_reference_missing_count"],
                    passed=False,
                    failure_reasons=json.dumps(
                        qa["hard_fail_reasons"] + qa["warnings"]
                    ),
                    generated_at=_now(),
                ))
            msg = (
                f"BATCH QA REFUSED: {len(qa['hard_fail_reasons'])} hard fail "
                f"reason(s); prior review_queue.csv and email_drafts left "
                f"intact. Reasons: {'; '.join(qa['hard_fail_reasons'])}"
            )
            print(f"[stage 7] {msg}")
            for hf in qa["hard_fail_reasons"]:
                print(f"[stage 7] HARD FAIL: {hf}")
            for w in qa["warnings"]:
                print(f"[stage 7] WARN: {w}")
            run.note(msg)
            run.failed = max(run.failed, 1)
            return 2

        # ---- persistence ----
        # Stale-state invalidation (Findings 11, 115, 116):
        # Only delete prior drafts for partners we ACTUALLY generated new
        # output for. If LLM generation crashed for partner X mid-batch,
        # X's old drafts must not be wiped without replacement -- otherwise
        # a flaky LLM run silently erases the operator's prior usable
        # batch for those partners.
        # Batch 37 (#38): additionally PRESERVE prior drafts when this
        # run's RECOMMENDED draft for the same partner has per-draft
        # hard-gate failures. Otherwise a bad regeneration would
        # silently replace a good prior draft. The partner's downgrade
        # to outreach_status=draft already lands in the CSV reasoning;
        # this just refuses to nuke the historical email_drafts row.
        partner_ids_with_failed_rec: set[str] = set()
        for pctx, output, _ in partner_outputs:
            rec_strategy = output.recommended_variant_strategy
            if not rec_strategy:
                continue
            for v in output.variants:
                if v.strategy != rec_strategy:
                    continue
                hf = check_hard_gates(
                    {"subject": v.subject, "body": v.body}, banned,
                )
                if hf:
                    partner_ids_with_failed_rec.add(pctx["partner_id"])
                break
        partner_ids_in_batch = [
            pctx["partner_id"] for pctx, _output, _ in partner_outputs
            if pctx["partner_id"] not in partner_ids_with_failed_rec
        ]
        if partner_ids_with_failed_rec:
            run.note(
                f"PRESERVED prior drafts for "
                f"{len(partner_ids_with_failed_rec)} partner(s) whose new "
                f"recommended draft failed per-draft hard gates: "
                f"{sorted(partner_ids_with_failed_rec)} (Batch 37 #38)"
            )
        with engine.begin() as conn:
            for pid in partner_ids_in_batch:
                conn.execute(delete(email_drafts).where(email_drafts.c.partner_id == pid))
                conn.execute(delete(followup_drafts).where(followup_drafts.c.partner_id == pid))
                conn.execute(delete(deck_request_responses).where(
                    deck_request_responses.c.partner_id == pid))

            # Index per-draft QA results by (partner_id, strategy).
            qa_by_key = {
                (d["partner_id"], d["strategy"]): d for d in all_drafts
            }
            for pctx, output, _ in partner_outputs:
                pid = pctx["partner_id"]
                for v in output.variants:
                    is_rec = (v.strategy == output.recommended_variant_strategy)
                    smell_info = qa_by_key.get((pid, v.strategy), {})
                    hard_fails = check_hard_gates(
                        {"subject": v.subject, "body": v.body}, banned
                    )
                    qa_status = "pass" if not hard_fails and smell_info.get("template_smell") != "high" else "fail"
                    # Batch 23 (#471/#472): leave written_to_csv_at NULL
                    # here. It's set AFTER write_review_queue() returns
                    # successfully, so a CSV failure no longer claims the
                    # rows landed in the CSV.
                    conn.execute(email_drafts.insert().values(
                        partner_id=pid,
                        batch_id=batch_id,
                        strategy=v.strategy,
                        subject=v.subject,
                        body=v.body,
                        conversion_hypothesis=v.conversion_hypothesis,
                        likely_objection=v.likely_objection,
                        objection_preempted=v.objection_preempted,
                        preemption_line=v.preemption_line,
                        template_smell=smell_info.get("template_smell", "unscored"),
                        qa_status=qa_status,
                        regeneration_count=0,
                        is_recommended=is_rec,
                        generated_at=_now(),
                        written_to_csv_at=None,
                    ))
                # Batch 23 (#473/#474): tag followup + deck with batch_id
                # so they can be reconciled to email_drafts.batch_id later.
                conn.execute(followup_drafts.insert().values(
                    partner_id=pid, batch_id=batch_id,
                    body=output.followup_draft,
                    generated_at=_now(),
                ))
                conn.execute(deck_request_responses.insert().values(
                    partner_id=pid, batch_id=batch_id,
                    body=output.deck_request_response,
                    generated_at=_now(),
                ))
            conn.execute(batch_qa_reports.insert().values(
                batch_id=batch_id,
                batch_size=len(all_drafts),
                # Batch 23 (#367/#467): partner count alongside the draft
                # count so the operator can reconcile both.
                batch_partner_count=len({d["partner_id"] for d in all_drafts}),
                strategy_distribution=json.dumps(qa["strategy_distribution"]),
                similarity_failures=qa["similarity_failure_count"],
                template_smell_high_count=qa["template_smell_high_count"],
                raise_reference_missing_count=qa["raise_reference_missing_count"],
                passed=qa["passed"],
                failure_reasons=json.dumps(qa["hard_fail_reasons"] + qa["warnings"]),
                generated_at=_now(),
            ))

        # ---- CSV write ----
        rec_by_partner = {
            d["partner_id"]: d for d in all_drafts if d.get("is_recommended")
        }
        alt_by_partner: dict[str, dict] = {}
        for d in all_drafts:
            if not d.get("is_recommended"):
                alt_by_partner[d["partner_id"]] = d

        # Pre-compute the set of partners whose recommended draft is in a
        # similarity-failure pair (Finding 1: don't mark such drafts
        # ready_to_send just because Stage 6 said so).
        sim_failed_partners: set[str] = set()
        for pid_a, pid_b, _kind, _score in qa["similarity_failures"]:
            sim_failed_partners.add(pid_a)
            sim_failed_partners.add(pid_b)

        # Build CSV rows.
        csv_rows: list[dict] = []
        downgraded_count = 0
        for pctx, output, _ in partner_outputs:
            pid = pctx["partner_id"]
            rec = rec_by_partner.get(pid)
            alt = alt_by_partner.get(pid)
            if not rec:
                continue
            p_signals = signals_by_partner.get(pid, [])
            top_signals_str = "\n".join(
                f'"{s["quote"]}" - {s["source_url"]} ({s["date"]})'
                for s in sorted(
                    p_signals, key=lambda s: s["quality"], reverse=True
                )[:3]
            )
            base = {
                "partner_id": pid,
                "partner_name": pctx["partner_name"],
                "partner_title": pctx["title"],
                "fund_name": pctx["fund_name"],
                "fund_domain": pctx["fund_domain"],
                "linkedin_url": pctx["linkedin_url"],
                "send_now_priority": round(pctx["send_now_priority"] or 0, 2),
                "composite_fit_score": round(pctx["composite_fit_score"] or 0, 2),
                "round_fit_score": pctx["round_fit_score"],
                "round_fit_reasoning": pctx["round_fit_reasoning"],
                "lead_likelihood_score": pctx["lead_likelihood_score"],
                "lead_likelihood_signals": pctx["lead_likelihood_signals"],
                "cold_reachability_score": pctx["cold_reachability_score"],
                "spiky_belief_score": round(pctx["spiky_belief_score"] or 0, 3),
                "top_signals": top_signals_str,
                "recommended_to_send": pctx["recommended_to_send"],
                "recommendation_reasoning": pctx["recommendation_reasoning"],
                "email_strategy_used": rec["strategy"],
                "email_subject_line": rec["subject"],
                "outreach_email_draft": rec["body"],
                "conversion_hypothesis": next(
                    v.conversion_hypothesis for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "likely_objection": next(
                    v.likely_objection for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "objection_preempted": next(
                    v.objection_preempted for v in output.variants
                    if v.strategy == rec["strategy"]
                ),
                "email_alternate_strategy": alt["strategy"] if alt else "",
                "email_draft_alternate": alt["body"] if alt else "",
                "followup_email_draft": output.followup_draft,
                "deck_request_response": output.deck_request_response,
                "template_smell": rec.get("template_smell", "unscored"),
                "warm_path_available": "" if pctx["warm_path_available"] is None else bool(pctx["warm_path_available"]),
            }
            # ---- outreach_status routing (Findings 1 + 3) ----
            # Compute per-draft hard-gate status. The draft was already
            # validated against the schema; recheck the body-level gates here
            # so we have the failure reasons available for the CSV.
            qa_fails: list[str] = check_hard_gates(
                {"subject": rec.get("subject"), "body": rec.get("body")}, banned
            )
            if rec.get("template_smell") == "high":
                qa_fails.append("template_smell=high")
            if pid in sim_failed_partners:
                qa_fails.append("body similarity > 0.82 with another draft")
            # Batch 9: production guards. A workspace scaffolded from a
            # fixture can have `.example` scheduling links, `{PLACEHOLDER}`
            # founder emails, or missing meeting_ask config. Downgrade
            # ready_to_send -> draft and surface the reasons so the
            # operator sees what to fix.
            prod_fails = production_gate_for_ready_to_send(
                subject=rec.get("subject"),
                body=rec.get("body"),
                scheduling_link=(
                    (ws.company.get("company") or {})
                    .get("meeting_ask", {})
                    .get("preferred_scheduling_link")
                ),
                founder_email=(ws.company.get("company") or {}).get(
                    "founder_email"
                ),
                partner_email=None,  # partner email is optional at CSV stage
                allow_example_domains=policy.allow_example_domains,
            )
            qa_fails.extend(prod_fails)
            # Batch 37 (#44): warn when an email body contains a URL that
            # looks like a scheduling link (cal.com, calendly.com,
            # savvycal.com, etc.) BUT doesn't match the workspace's
            # configured preferred_scheduling_link. Hallucinated scheduling
            # links route prospects to the wrong calendar.
            configured_link = (
                (ws.company.get("company") or {})
                .get("meeting_ask", {})
                .get("preferred_scheduling_link")
                or ""
            ).strip().lower()
            body_lower = (rec.get("body") or "").lower()
            scheduling_hosts = (
                "cal.com/", "calendly.com/", "savvycal.com/",
                "meetings.hubspot.com/", "cal.example/",
            )
            for host in scheduling_hosts:
                if host in body_lower and (
                    not configured_link or host not in configured_link
                ):
                    qa_fails.append(
                        f"body contains scheduling link host {host!r} but "
                        f"workspace configured "
                        f"{(configured_link or '<none>')!r} -- LLM may have "
                        f"hallucinated a scheduling URL"
                    )
                    break
            # Batch 37 (#42): defense in depth. Stage 6 already blocks
            # recommendation when cold_reachability_score is None, but
            # if a recommendation somehow lands here with no reachability
            # score (e.g. a force-rescore that ignored the gate), refuse
            # to mark it ready_to_send.
            if (
                pctx.get("recommended_to_send")
                and pctx.get("cold_reachability_score") is None
            ):
                qa_fails.append(
                    "cold_reachability_score is unknown; Stage 7 refuses "
                    "to mark ready_to_send without it"
                )
            # Batch 37 (#35): founder email domain should match the
            # company's primary domain when one is configured. Mismatch
            # is a soft warning (downgrade to draft) so the operator
            # catches "sending from gmail.com instead of company.io".
            founder_email = (
                ws.company.get("company") or {}
            ).get("founder_email") or ""
            primary_domain = _company_primary_domain(ws.company)
            if (
                founder_email
                and primary_domain
                and "@" in founder_email
            ):
                fe_domain = founder_email.split("@", 1)[1].lower().strip()
                if fe_domain != primary_domain:
                    qa_fails.append(
                        f"founder email domain {fe_domain!r} does not "
                        f"match company primary domain {primary_domain!r}; "
                        f"verify the sender is intentional"
                    )

            base["recommendation_reasoning"] = pctx["recommendation_reasoning"]
            if pctx.get("warm_path_available"):
                # Warm path takes precedence -- don't email cold.
                base["outreach_status"] = "warm_path_needed"
                base["recommendation_reasoning"] = (
                    f"warm_path_available=TRUE; cold draft suppressed. "
                    f"{pctx['recommendation_reasoning'] or ''}"
                ).strip()
            elif pctx["recommended_to_send"] and qa_fails:
                base["outreach_status"] = "draft"
                base["recommendation_reasoning"] = (
                    f"DOWNGRADED by Stage 7 QA: {'; '.join(qa_fails)}. "
                    f"(Stage 6 said: {pctx['recommendation_reasoning'] or '-'})"
                )
                downgraded_count += 1
            elif pctx["recommended_to_send"]:
                base["outreach_status"] = "ready_to_send"
            else:
                base["outreach_status"] = "draft"
            csv_rows.append(base)

        out_path = write_review_queue(ws.exports_dir, csv_rows)

        # Batch 23 (#471/#472): stamp written_to_csv_at on the recommended
        # email_drafts rows AFTER the CSV write returned successfully.
        # If write_review_queue() had raised, none of these rows would
        # claim they were written.
        rec_partner_ids = [
            r["partner_id"] for r in csv_rows
            if r.get("outreach_status") == "ready_to_send"
            or r.get("outreach_status") == "draft"
        ]
        if rec_partner_ids:
            now = _now()
            with engine.begin() as conn:
                conn.execute(
                    email_drafts.update()
                    .where(
                        email_drafts.c.batch_id == batch_id,
                        email_drafts.c.is_recommended.is_(True),
                        email_drafts.c.partner_id.in_(rec_partner_ids),
                    )
                    .values(written_to_csv_at=now)
                )

        ready = sum(1 for r in csv_rows if r["outreach_status"] == "ready_to_send")
        warm_routed = sum(
            1 for r in csv_rows if r["outreach_status"] == "warm_path_needed"
        )
        print(
            f"[stage 7] {len(csv_rows)} CSV row(s) -> {out_path} "
            f"(ready_to_send={ready}, warm_path_needed={warm_routed}, "
            f"draft={len(csv_rows) - ready - warm_routed})"
        )
        if downgraded_count:
            # Finding 1: a Stage-6 recommended partner whose generated draft
            # failed QA must NOT land as ready_to_send. Surface the count
            # loudly so the operator looks at the reasoning column.
            print(
                f"[stage 7] DOWNGRADED {downgraded_count} recommended "
                f"partner(s) to draft due to per-draft QA failures "
                f"(see recommendation_reasoning column)."
            )
            run.note(f"downgraded_to_draft={downgraded_count}")
        print(
            f"[stage 7] batch QA: passed={qa['passed']} | "
            f"similarity failures={qa['similarity_failure_count']} | "
            f"template_smell=high {qa['template_smell_high_count']} | "
            f"strategy distribution={qa['strategy_distribution']}"
        )
        for w in qa["warnings"]:
            print(f"[stage 7] WARN: {w}")
        for hf in qa["hard_fail_reasons"]:
            print(f"[stage 7] HARD FAIL: {hf}")
        print(f"[stage 7] llm stub mode: {llm.stub}")

    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
