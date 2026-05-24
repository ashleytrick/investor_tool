"""Stage 7 batch QA: per-draft hard gates + cross-batch similarity / smell
analysis (Refactor item 7 / 14).

Pure functions extracted from scripts/07_generate_emails.py. Behavior
unchanged. Stage 7 calls evaluate_batch() once after generating every
partner's drafts; per-draft hard gates fire per generated variant
during the persistence pass.

Thresholds live as module-level constants so any operator-tunable
gate is visible on this page rather than as magic literals.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from core.similarity import first_sentence, ratio_similarity, token_set_similarity


# Similarity hard gates: a recommended draft pair above these scores
# constitutes a batch-QA hard fail (the batch is rejected so the
# operator regenerates).
SIM_BODY_HARD = 0.82
SIM_FIRST_HARD = 0.70
SIM_SUBJECT_HARD = 0.75

# Soft warnings (don't fail the batch, but the operator should see).
WARN_STRATEGY_SHARE = 0.35
WARN_FIRST_SENT_SHARE = 0.25
WARN_CTA_SHARE = 0.20
# Target share of drafts that should land in template_smell="low".
WARN_TEMPLATE_LOW_SHARE = 0.80

# Smell-judge thresholds (heuristic stub used when an LLM judge isn't
# wired up).
SMELL_HIGH_BODY_SIM = 0.82
SMELL_MEDIUM_BODY_SIM = 0.80
SMELL_TOO_SIMILAR_SIM = 0.78
SMELL_MASS_FIRST_SIM = 0.70


# Forbidden phrases (universal + founder-voice-banned passed in by
# caller). Anything in this list, when found in a draft body
# (case-insensitive), fails the per-draft hard gate.
UNIVERSAL_FORBIDDEN: tuple[str, ...] = (
    "building the future of", "would love", "circling back",
    "wanted to reach out", "hope this finds you", "quick question",
    "pressure-test", "compare notes", "thesis chat", "get your feedback",
    "synergy", "game-changing", "excited to",
)
SOFT_CTA_PHRASES: tuple[str, ...] = (
    "thesis chat", "feedback", "pressure-test", "compare notes",
    "grab coffee", "would love to chat",
)

_RAISE_RE = re.compile(
    r"\b(raising|raise|seed round|series [a-z])\b", re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def check_hard_gates(
    draft: dict, banned: Iterable[str] = (),
) -> list[str]:
    """Per-draft hard gates. Returns a list of human-readable failure
    reasons; an empty list means the draft passed.

    `banned` is the workspace's founder-voice banned-phrases list,
    layered on top of UNIVERSAL_FORBIDDEN.
    """
    fails: list[str] = []
    body = draft.get("body") or ""
    body_lower = body.lower()
    # Finding 6: refuse literal `{X}` placeholders the model might have
    # emitted (TIME_1/TIME_2 are the obvious ones, but the gate catches
    # any uppercase-token placeholder so future prompt changes can't slip).
    leftover = _PLACEHOLDER_RE.findall(body)
    if leftover:
        fails.append(
            f"unfilled prompt placeholder(s) in body: {sorted(set(leftover))}"
        )
    # Word-boundary match so "$3M raise." and "Seed round closing" both count.
    if not _RAISE_RE.search(body):
        fails.append("missing explicit raise reference in body")
    if any(p in body_lower for p in SOFT_CTA_PHRASES):
        fails.append("soft CTA phrase present")
    for ph in tuple(UNIVERSAL_FORBIDDEN) + tuple(banned):
        if ph and ph.lower() in body_lower:
            fails.append(f"forbidden phrase: {ph!r}")
    if "—" in body:
        fails.append("em dash in body")
    if "!" in body:
        fails.append("exclamation mark in body")
    return fails


def template_smell_judge(
    draft_body: str, neighbor_bodies: list[str],
) -> tuple[str, bool, bool]:
    """Heuristic stub judge. Returns (smell, sounds_mass_generated,
    too_similar).

    `high` is reserved for near-duplicates above the body hard gate
    (SMELL_HIGH_BODY_SIM). The judge promotes to `medium` when a draft
    shares its opening structure with a neighbor (the brief's
    "same first-sentence structural pattern" warning) or sits in the
    SMELL_MEDIUM_BODY_SIM..SMELL_HIGH_BODY_SIM band. Token-set
    similarity in a tight single-company batch will inherently run in
    the 0.60-0.78 range due to shared CTA + product vocabulary; that
    range is `low`.
    """
    if not neighbor_bodies:
        return "low", False, False
    body_sims = [
        token_set_similarity(draft_body, n) for n in neighbor_bodies if n
    ]
    fs_a = first_sentence(draft_body)
    fs_sims = [
        ratio_similarity(fs_a, first_sentence(n))
        for n in neighbor_bodies if n
    ]
    max_body = max(body_sims) if body_sims else 0.0
    max_first = max(fs_sims) if fs_sims else 0.0
    too_similar = max_body > SMELL_TOO_SIMILAR_SIM
    mass = max_first > SMELL_MASS_FIRST_SIM
    if max_body > SMELL_HIGH_BODY_SIM:
        return "high", mass, True
    if mass or max_body > SMELL_MEDIUM_BODY_SIM:
        return "medium", mass, too_similar
    return "low", mass, too_similar


def evaluate_batch(
    recommended_drafts: list[dict],
    all_drafts: list[dict],
) -> dict:
    """Compute similarity, template_smell, and gate report for the batch.

    Mutates `all_drafts` to attach template_smell /
    sounds_mass_generated / too_similar_to_neighbors keys on each
    dict (Stage 7 reads these when persisting per-draft email_drafts
    rows).
    """
    # Similarity check across recommended drafts.
    sim_failures: list[tuple[str, str, str, float]] = []
    bodies = [(d["partner_id"], d["body"]) for d in recommended_drafts]
    subjects = [
        (d["partner_id"], d.get("subject") or "") for d in recommended_drafts
    ]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            sb = token_set_similarity(bodies[i][1], bodies[j][1])
            if sb > SIM_BODY_HARD:
                sim_failures.append((bodies[i][0], bodies[j][0], "body", sb))
            fa = first_sentence(bodies[i][1])
            fb = first_sentence(bodies[j][1])
            sf = ratio_similarity(fa, fb)
            if sf > SIM_FIRST_HARD:
                sim_failures.append(
                    (bodies[i][0], bodies[j][0], "first_sentence", sf)
                )
            ss = ratio_similarity(subjects[i][1], subjects[j][1])
            if ss > SIM_SUBJECT_HARD:
                sim_failures.append(
                    (subjects[i][0], subjects[j][0], "subject", ss)
                )

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

    smell_high_count = sum(
        1 for d in all_drafts if d["template_smell"] == "high"
    )
    smell_low_count = sum(
        1 for d in all_drafts if d["template_smell"] == "low"
    )
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
        hard_fail_reasons.append(
            f"{len(sim_failures)} similarity gate failure(s)"
        )
    if smell_high_count:
        hard_fail_reasons.append(
            f"{smell_high_count} draft(s) template_smell=high"
        )
    if raise_missing:
        hard_fail_reasons.append(
            f"{raise_missing} draft(s) missing raise reference"
        )

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
