"""Stub email bank (Refactor item 14).

Used when no ANTHROPIC_API_KEY is resolvable so Stage 7 still produces
deterministic variants for the fixture partners. The live LLM path
(prompts/generate_email.txt) handles arbitrary real partners; this
bank is the offline alternative for tests + smoke runs.

The DECK_RESPONSE_TEMPLATE is shared by every partner; EMAIL_BANK is
keyed on partner_id (deterministic-slug form) and contains
strategy-specific variants. build_stub_response() assembles the
EmailOutput-shaped dict the LLMClient stub path expects.
"""
from __future__ import annotations

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


