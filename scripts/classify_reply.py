"""Classify a reply email into one of the brief's 9 reply_type buckets and
record it as an outcome.

Always asks for operator confirmation before writing -- the brief is clear
that mis-categorized outcomes pollute the learning loop worse than no
outcomes at all. Stub mode (no API key) uses a keyword heuristic; live mode
uses the LLM.

Examples:
  uv run scripts/classify_reply.py --partner-id NAME --file reply.eml
  uv run scripts/classify_reply.py --partner-id NAME --text "thanks but pass"
  cat reply.eml | uv run scripts/classify_reply.py --partner-id NAME --stdin
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import get_engine, outcomes, partners
from core.llm.client import MODEL_BATCH, LLMClient
from core.runs import RunLogger
from schemas.reply_classification import ReplyClassification

STAGE = "classify_reply"
PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "classify_reply.txt"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- stub-mode heuristic ---

_HEURISTIC_PATTERNS = [
    # most specific first
    (re.compile(r"\bout of\s*office\b|\bauto[- ]?reply\b|\bvacation\b",
                re.IGNORECASE), "no_response", "high",
     "auto-responder / out-of-office pattern"),
    (re.compile(
        r"\bcalendar\b|\bbook\s*a?\s*(time|slot|call|meeting)\b|"
        r"\bcalend(?:ly|ar)\b|\bworks for me\b|\bsounds good[.,]?\s*(let\'s|let us)\b|"
        r"\bhappy to (chat|meet|talk)\b",
        re.IGNORECASE), "booked", "medium",
     "language suggesting agreement to meet"),
    (re.compile(
        r"\bsend (me )?(the )?deck\b|\bone[- ]?pager\b|\bpitch\s*deck\b|\bshare (a |the )?deck\b",
        re.IGNORECASE), "asked_for_deck", "high",
     "explicit deck/one-pager request"),
    (re.compile(
        r"\btoo early\b|\bpre[- ]?revenue\b|\bcome back when\b",
        re.IGNORECASE), "passed_too_early", "medium",
     "stage/maturity-based pass"),
    (re.compile(
        r"\bnot (our|a) (space|focus|fit|area|category|sector)\b|"
        r"\bdon[\'’]t (do|invest in)\b",
        re.IGNORECASE), "passed_category", "medium",
     "category-based pass"),
    (re.compile(
        r"\bseries [b-z]\b.*\b(only|focus|stage)\b|\blater[- ]?stage only\b|"
        r"\bwe lead\b.+\bonly\b",
        re.IGNORECASE), "wrong_stage", "medium",
     "stage-mismatch language"),
    (re.compile(
        r"\bmetrics\b|\barr\b|\bgrowth\b|\bretention\b|\bmore (info|detail|color)\b|"
        r"\b(can|could) (you )?share\b|\bbreak(down|out)\b",
        re.IGNORECASE), "asked_for_more_info", "low",
     "engaged but asking for specifics"),
    (re.compile(
        r"\b(introduce|intro|connect)\b.*\b(my partner|colleague|teammate|associate)\b|"
        r"\b(you should|please) (talk to|reach out to|chat with)\b",
        re.IGNORECASE), "referred_to_colleague", "medium",
     "redirect to another partner/colleague"),
    (re.compile(
        r"\bwarm intro\b|\b(who|anyone) can intro\b|\bmutual connection\b",
        re.IGNORECASE), "warm_intro_requested", "medium",
     "warm-intro request"),
]


def _heuristic_classify(text: str) -> ReplyClassification:
    for pattern, reply_type, conf, why in _HEURISTIC_PATTERNS:
        if pattern.search(text):
            return ReplyClassification(
                reply_type=reply_type,
                confidence=conf,
                reasoning=f"stub heuristic: {why}",
                meeting_booked=(reply_type == "booked"),
            )
    return ReplyClassification(
        reply_type="asked_for_more_info",
        confidence="low",
        reasoning="stub heuristic: no clear pattern matched; defaulting to "
                  "asked_for_more_info pending operator review",
        meeting_booked=False,
    )


def classify(llm: LLMClient, *, text: str, company_name: str, round_name: str,
             duration_min: int) -> ReplyClassification:
    if llm.stub:
        return _heuristic_classify(text)
    prompt = (
        PROMPT_PATH.read_text(encoding="utf-8")
        .replace("{REPLY_TEXT}", text)
        .replace("{COMPANY_NAME}", company_name)
        .replace("{ROUND}", round_name)
        .replace("{MEETING_DURATION}", str(duration_min))
    )
    return llm.complete_json(
        prompt=prompt,
        schema=ReplyClassification,
        model=MODEL_BATCH,
        stub_response=_heuristic_classify(text).model_dump(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify a reply -> record outcome.")
    add_workspace_arg(parser)
    parser.add_argument("--partner-id", required=True)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", default=None, help="Reply text inline.")
    src.add_argument("--file", default=None, help="Read reply from file.")
    src.add_argument("--stdin", action="store_true", help="Read reply from stdin.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt (use carefully; the "
                             "brief explicitly warns against unconfirmed "
                             "automated classification).")
    args = parser.parse_args()

    if args.file:
        text = pathlib.Path(args.file).read_text(encoding="utf-8")
    elif args.stdin:
        text = sys.stdin.read()
    else:
        text = args.text
    if not (text or "").strip():
        print("[classify_reply] empty reply text; aborting")
        return 2

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)
    print_banner(ws, stage=STAGE)

    with engine.begin() as conn:
        partner = conn.execute(
            select(partners.c.partner_id, partners.c.name).where(
                partners.c.partner_id == args.partner_id
            )
        ).first()
    if not partner:
        print(f"[classify_reply] unknown partner_id: {args.partner_id!r}")
        return 2

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        run.processed = 1
        company_cfg = ws.company or {}
        result = classify(
            llm, text=text,
            company_name=(company_cfg.get("company") or {}).get("name", "?"),
            round_name=(company_cfg.get("raise_context") or {}).get("round", "?"),
            duration_min=int(
                (company_cfg.get("company") or {})
                .get("meeting_ask", {}).get("duration_minutes", 30)
            ),
        )

        print()
        print(f"  partner:          {partner.name} ({partner.partner_id})")
        print(f"  reply_type:       {result.reply_type}")
        print(f"  confidence:       {result.confidence}")
        print(f"  meeting_booked:   {result.meeting_booked}")
        print(f"  reasoning:        {result.reasoning}")
        print()

        if not args.yes:
            ans = input("Record this outcome? [y/N/edit] ").strip().lower()
            if ans not in ("y", "yes", "edit"):
                print("[classify_reply] aborted; nothing written")
                run.skipped = 1
                return 0
            if ans == "edit":
                new_type = input(
                    "  reply_type override (blank to keep): "
                ).strip() or None
                if new_type:
                    try:
                        result = result.model_copy(update={"reply_type": new_type})
                    except Exception as exc:  # noqa: BLE001
                        print(f"  invalid reply_type: {exc}; keeping {result.reply_type!r}")

        outreach_status = (
            "meeting_booked" if result.meeting_booked
            else "replied" if result.reply_type != "no_response"
            else "sent"
        )
        with engine.begin() as conn:
            conn.execute(outcomes.insert().values(
                partner_id=args.partner_id,
                outreach_status=outreach_status,
                reply_type=result.reply_type,
                meeting_booked=bool(result.meeting_booked),
                synced_from_attio_at=_now(),
                source="manual",
            ))
        run.succeeded = 1
        run.note(
            f"classified {args.partner_id} -> {result.reply_type} "
            f"({result.confidence}); meeting_booked={result.meeting_booked}"
        )
        print(f"[classify_reply] recorded outcome for {args.partner_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
