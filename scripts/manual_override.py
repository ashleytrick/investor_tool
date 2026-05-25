"""Set or clear the manual override flags on a partner without touching SQL.

The CSV review queue is read-only from the operator's side (it gets overwritten
by each Stage 7 run). To express judgment ("don't re-score this partner",
"force-promote this one to ready_to_send", "warm path exists, don't cold this
person") the operator needs to flip flags in partner_score_summaries /
partners. This script is the supported interface; routine Stage 6 runs respect
the flags it sets and Stage 7 honors warm_path_available.

Examples:
  # Pin scores on a partner you hand-tuned. Freezes composite, round_fit,
  # lead_likelihood, send_now_priority, etc.; routine Stage 6 skips this
  # partner's score fields until you --clear or --force-rescore.
  uv run scripts/manual_override.py --partner-id NAME --score \\
      --reason "hand-curated after meeting"

  # Force-promote OR force-demote recommended_to_send (BOTH sets the value
  # AND sets manual_recommended_override=True so Stage 6 leaves it alone).
  uv run scripts/manual_override.py --partner-id NAME --recommend yes \\
      --reason "champion at fund; bypass criterion 4"
  uv run scripts/manual_override.py --partner-id NAME --recommend no \\
      --reason "verified left fund last week; don't email"

  # Mark warm path; Stage 7 emits outreach_status=warm_path_needed in the CSV.
  uv run scripts/manual_override.py --partner-id NAME --warm-path \\
      --warm-path-contact "ashley@... knows them" \\
      --reason "warm intro available"

  # Clear all overrides on a partner.
  uv run scripts/manual_override.py --partner-id NAME --clear

  # Inspect what's overridden across the workspace.
  uv run scripts/manual_override.py --list
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.config_loader import add_workspace_arg
from core.db import partner_score_summaries, partners
from core.operator_command import operator_command_run

STAGE = "manual_override"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set/clear manual overrides.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="List all partners with any override flag set.")
    g.add_argument("--clear", action="store_true",
                   help="Clear overrides on --partner-id. By default clears "
                        "score + recommendation + warm-path. Use "
                        "--clear-score / --clear-rec / --clear-warm to limit "
                        "scope (Batch 15 #287).")
    g.add_argument("--score", action="store_true",
                   help="Set manual_score_override=TRUE on --partner-id.")
    g.add_argument("--recommend", choices=("yes", "no"),
                   help="Force-promote (yes) or force-demote (no) "
                        "recommended_to_send on --partner-id. Sets both the "
                        "value AND manual_recommended_override=True so Stage 6 "
                        "leaves it alone going forward.")
    g.add_argument("--warm-path", action="store_true",
                   help="Set partners.warm_path_available=TRUE on --partner-id. "
                        "Stage 7 will emit outreach_status=warm_path_needed. "
                        "Requires --warm-path-contact (Batch 15 #290).")

    parser.add_argument("--partner-id", default=None,
                        help="Target partner_id (required for non-list ops).")
    parser.add_argument("--reason", default=None,
                        help="Required for --score / --recommended / --warm-path.")
    parser.add_argument("--warm-path-contact", default=None,
                        help="REQUIRED for --warm-path: who has the warm intro. "
                             "Warm-path routing is unactionable without contact "
                             "detail; the override is rejected if omitted.")
    # Batch 15 #287: per-flag scoping for --clear so the operator doesn't
    # accidentally wipe the warm-path side when they only wanted to drop
    # the score lock.
    parser.add_argument("--clear-score", action="store_true",
                        help="With --clear: only clear manual_score_override.")
    parser.add_argument("--clear-rec", action="store_true",
                        help="With --clear: only clear manual_recommended_override.")
    parser.add_argument("--clear-warm", action="store_true",
                        help="With --clear: only clear warm_path_available.")
    args = parser.parse_args()

    if not args.list and not args.partner_id:
        parser.error("--partner-id is required unless --list")
    requires_reason = args.score or bool(args.recommend) or args.warm_path
    if requires_reason and not args.reason:
        parser.error("--reason is required when setting an override")
    # Batch 15 #290: warm-path is hard to act on without contact detail.
    if args.warm_path and not (args.warm_path_contact or "").strip():
        parser.error(
            "--warm-path requires --warm-path-contact (e.g. "
            "'ashley@example.com knows them via Series A board')"
        )
    if (args.clear_score or args.clear_rec or args.clear_warm) and not args.clear:
        parser.error(
            "--clear-score / --clear-rec / --clear-warm require --clear"
        )

    with operator_command_run(args, stage=STAGE) as ctx:
        engine, run = ctx.engine, ctx.run
        if args.list:
            # Findings 50, 55: show the FROZEN values + freshness so the
            # operator can see WHAT they pinned and how stale it is.
            with engine.begin() as conn:
                summary_rows = list(conn.execute(
                    select(
                        partner_score_summaries.c.partner_id,
                        partner_score_summaries.c.manual_score_override,
                        partner_score_summaries.c.manual_recommended_override,
                        partner_score_summaries.c.manual_override_reason,
                        partner_score_summaries.c.composite_fit_score,
                        partner_score_summaries.c.round_fit_score,
                        partner_score_summaries.c.lead_likelihood_score,
                        partner_score_summaries.c.recommended_to_send,
                        partner_score_summaries.c.scored_at,
                    ).where(
                        (partner_score_summaries.c.manual_score_override.is_(True))
                        | (partner_score_summaries.c.manual_recommended_override.is_(True))
                    )
                ))
                warm_rows = list(conn.execute(
                    select(
                        partners.c.partner_id, partners.c.name,
                        partners.c.warm_path_contact,
                    ).where(partners.c.warm_path_available.is_(True))
                ))
            if not summary_rows and not warm_rows:
                print("[overrides] none set in this workspace")
            for r in summary_rows:
                flags = []
                if r.manual_score_override:
                    flags.append("score")
                if r.manual_recommended_override:
                    flags.append("recommended")
                age = "?"
                if r.scored_at:
                    try:
                        delta = datetime.now(timezone.utc) - r.scored_at.replace(
                            tzinfo=timezone.utc
                        )
                        age = f"{delta.days}d ago"
                    except (AttributeError, TypeError):
                        age = str(r.scored_at)
                print(
                    f"[overrides] {r.partner_id}: {'+'.join(flags)} | "
                    f"frozen composite={r.composite_fit_score} "
                    f"round_fit={r.round_fit_score} "
                    f"lead={r.lead_likelihood_score} "
                    f"recommended={r.recommended_to_send} | "
                    f"scored_at={age} | "
                    f"reason={r.manual_override_reason!r}"
                )
            for r in warm_rows:
                print(
                    f"[overrides] {r.partner_id} ({r.name}): warm_path | "
                    f"contact={r.warm_path_contact!r}"
                )
            run.processed = len(summary_rows) + len(warm_rows)
            run.succeeded = run.processed
            return 0

        pid = args.partner_id
        run.processed = 1

        if args.warm_path:
            with engine.begin() as conn:
                existing = conn.execute(
                    select(partners.c.partner_id).where(partners.c.partner_id == pid)
                ).first()
                if not existing:
                    print(f"[overrides] partner {pid!r} not found in partners table")
                    run.failed = 1
                    run.log_error(pid, "not_found", "no such partner")
                    return 2
                conn.execute(
                    partners.update().where(partners.c.partner_id == pid).values(
                        warm_path_available=True,
                        warm_path_contact=args.warm_path_contact,
                        last_updated=_now(),
                    )
                )
                # Batch 15 #288/#289: persist the warm-path reason in the
                # partner-summary override reason so the warm-path rationale
                # isn't lost when score/rec overrides also get set. Reasons
                # are namespaced "warm: ...", "score: ...", "rec: ..." so
                # multiple types can coexist.
                _append_override_reason(
                    conn, pid, "warm", args.reason,
                )
            print(f"[overrides] {pid}: warm_path=TRUE; reason logged: {args.reason!r}")
            run.note(f"warm_path set on {pid}: {args.reason!r}")
            run.succeeded = 1
            return 0

        if args.clear:
            # Finding 49: confirm the partner actually exists before claiming
            # success. A typo would previously affect zero rows and still
            # exit 0.
            with engine.begin() as conn:
                exists = conn.execute(
                    select(partners.c.partner_id).where(
                        partners.c.partner_id == pid
                    )
                ).first()
                if not exists:
                    print(
                        f"[overrides] partner {pid!r} not found; nothing cleared."
                    )
                    run.failed = 1
                    run.log_error(pid, "not_found", "no such partner")
                    return 2
                # Batch 15 #287: per-flag scoping. If none of the
                # --clear-* flags are set, behave like the old global clear
                # (drop everything). Otherwise drop only the selected slices.
                clear_all = not (
                    args.clear_score or args.clear_rec or args.clear_warm
                )
                summary_updates: dict = {}
                if clear_all or args.clear_score:
                    summary_updates["manual_score_override"] = False
                if clear_all or args.clear_rec:
                    summary_updates["manual_recommended_override"] = False
                # Reset the reason only when ALL slices are cleared, or when
                # both score+rec are explicitly cleared. Warm-path reason
                # lives alongside in the same field; namespaced clearing
                # keeps non-cleared slices' reasons.
                if clear_all:
                    summary_updates["manual_override_reason"] = None
                elif args.clear_score and args.clear_rec:
                    # Drop score+rec namespaces; preserve warm.
                    _drop_override_reason_namespaces(
                        conn, pid, drop=("score", "rec"),
                    )
                elif args.clear_score:
                    _drop_override_reason_namespaces(
                        conn, pid, drop=("score",),
                    )
                elif args.clear_rec:
                    _drop_override_reason_namespaces(
                        conn, pid, drop=("rec",),
                    )
                if summary_updates:
                    conn.execute(
                        partner_score_summaries.update()
                        .where(partner_score_summaries.c.partner_id == pid)
                        .values(**summary_updates)
                    )
                if clear_all or args.clear_warm:
                    conn.execute(
                        partners.update().where(partners.c.partner_id == pid)
                        .values(
                            warm_path_available=None,
                            warm_path_contact=None,
                        )
                    )
                    if not clear_all:
                        # Targeted warm clear: drop only that namespace from
                        # the summary reason.
                        _drop_override_reason_namespaces(
                            conn, pid, drop=("warm",),
                        )
            label = (
                "all overrides cleared" if clear_all
                else "cleared: " + "+".join(
                    n for n, on in (
                        ("score", args.clear_score),
                        ("rec", args.clear_rec),
                        ("warm", args.clear_warm),
                    ) if on
                )
            )
            print(f"[overrides] {pid}: {label}")
            run.note(f"{label} on {pid}")
            run.succeeded = 1
            return 0

        # --score or --recommend
        if args.score:
            with engine.begin() as conn:
                _append_override_reason(conn, pid, "score", args.reason)
                conn.execute(
                    partner_score_summaries.update()
                    .where(partner_score_summaries.c.partner_id == pid)
                    .values(manual_score_override=True)
                )
            label = "manual_score_override=TRUE"
            print(f"[overrides] {pid}: {label}; reason={args.reason!r}")
            run.note(f"{label} on {pid}: {args.reason!r}")
            run.succeeded = 1
            return 0
        # --recommend yes|no: BOTH set the value AND lock it (Finding 2).
        new_value = (args.recommend == "yes")
        update = {
            "manual_recommended_override": True,
            "recommended_to_send": new_value,
            "recommendation_reasoning": (
                f"manual override ({args.recommend}): {args.reason}"
            ),
        }
        label = (
            f"recommended_to_send={new_value} + "
            f"manual_recommended_override=TRUE"
        )
        with engine.begin() as conn:
            existing = conn.execute(
                select(partner_score_summaries.c.partner_id).where(
                    partner_score_summaries.c.partner_id == pid
                )
            ).first()
            if not existing:
                print(
                    f"[overrides] partner_score_summaries row not found for "
                    f"{pid!r}. Run Stage 6 first."
                )
                run.failed = 1
                run.log_error(pid, "not_found", "no summary row")
                return 2
            conn.execute(
                partner_score_summaries.update()
                .where(partner_score_summaries.c.partner_id == pid)
                .values(**update)
            )
            _append_override_reason(conn, pid, "rec", args.reason)
        print(f"[overrides] {pid}: {label}; reason={args.reason!r}")
        run.note(f"{label} on {pid}: {args.reason!r}")
        run.succeeded = 1

    return 0


def _append_override_reason(conn, pid: str, namespace: str, reason: str) -> None:
    """Append a namespaced reason to manual_override_reason without losing
    previously-set reasons for other namespaces. Batch 15 #288: the field
    used to be wholesale-overwritten so a `--score` after a `--warm-path`
    lost the warm-path rationale. We now store reasons as `"score: ...;
    warm: ..."` so each type's justification survives the others.

    Replaces the existing entry for `namespace` if one exists.
    """
    row = conn.execute(
        select(partner_score_summaries.c.manual_override_reason)
        .where(partner_score_summaries.c.partner_id == pid)
    ).first()
    if row is None:
        # No summary row yet (warm-path on a partner without Stage 6); skip
        # write -- the warm-path branch writes to partners only.
        return
    current = (row.manual_override_reason or "").strip()
    entries: dict[str, str] = {}
    if current:
        # Parse "name: text; name: text"
        for part in current.split(";"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                k, v = part.split(":", 1)
                entries[k.strip()] = v.strip()
            else:
                entries.setdefault("_legacy", part)
    entries[namespace] = reason
    merged = "; ".join(f"{k}: {v}" for k, v in entries.items())
    conn.execute(
        partner_score_summaries.update()
        .where(partner_score_summaries.c.partner_id == pid)
        .values(manual_override_reason=merged)
    )


def _drop_override_reason_namespaces(
    conn, pid: str, drop: tuple[str, ...]
) -> None:
    """Remove the named namespaces from manual_override_reason; preserve
    the rest. Used by --clear with --clear-* scoping (Batch 15 #287)."""
    row = conn.execute(
        select(partner_score_summaries.c.manual_override_reason)
        .where(partner_score_summaries.c.partner_id == pid)
    ).first()
    if row is None or not (row.manual_override_reason or "").strip():
        return
    entries: dict[str, str] = {}
    for part in row.manual_override_reason.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
            entries[k.strip()] = v.strip()
    for ns in drop:
        entries.pop(ns, None)
    merged = "; ".join(f"{k}: {v}" for k, v in entries.items()) or None
    conn.execute(
        partner_score_summaries.update()
        .where(partner_score_summaries.c.partner_id == pid)
        .values(manual_override_reason=merged)
    )


if __name__ == "__main__":
    raise SystemExit(main())
