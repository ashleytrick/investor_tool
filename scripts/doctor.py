"""DB invariant doctor: read-only checks that surface integrity drift.

Run: uv run python scripts/doctor.py [--workspace clients/foo]

Each check returns a list of (severity, message) tuples. Severity is one
of 'error' / 'warn' / 'info'. The doctor exits 2 if any errors found,
1 if only warnings, 0 if clean.

This is read-only; it never mutates. Operators run it before / after
running a stage, or on a cron alongside `status.py`, to catch:

  - orphan rows (FK was added in Batch 6 for new DBs; older DBs may
    still have orphans because SQLite doesn't retroactively enforce FKs)
  - score values outside [0, 10]
  - future-dated signals / deals / outcomes that snuck past Batch 10
    schema validation (e.g. rows from older runs)
  - placeholders left in fields the operator was supposed to edit
  - verified signals lacking a quality score / unverified signals
    carrying one (Stage 5 hygiene drift)
  - duplicate pending axis suggestions for the same axis
  - warm-path partners marked ready_to_send in the latest CSV
  - missing recommended drafts for partners with recommended_to_send=TRUE
  - axis scores keyed off axis_ids not present in axes.yaml

Each finding includes the SQL the operator can run to inspect / fix it.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import (
    axis_weight_suggestions,
    deal_attributions,
    email_drafts,
    funds,
    get_engine,
    manual_override_events,
    outcomes,
    partner_score_summaries,
    partners,
    scores,
    signals,
    source_snapshots,
    sources,
)

Severity = str  # "error" | "warn" | "info"

# Placeholder pattern reused from validate_config: `{TOKEN}` style.
import re
_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _check_orphan_summaries(engine) -> list[tuple[Severity, str]]:
    """502, 504, 507: every score row + summary row references a real partner."""
    out: list[tuple[Severity, str]] = []
    with engine.begin() as conn:
        # Orphan summaries
        n = conn.execute(
            select(func.count()).select_from(partner_score_summaries)
            .where(~partner_score_summaries.c.partner_id.in_(
                select(partners.c.partner_id)
            ))
        ).scalar()
        if n:
            out.append((
                "error",
                f"{n} partner_score_summaries row(s) reference a partner_id "
                f"not in partners (orphan; older DB without FK enforcement). "
                f"SQL: SELECT partner_id FROM partner_score_summaries WHERE "
                f"partner_id NOT IN (SELECT partner_id FROM partners);",
            ))
        # Orphan scores
        n = conn.execute(
            select(func.count()).select_from(scores)
            .where(~scores.c.partner_id.in_(select(partners.c.partner_id)))
        ).scalar()
        if n:
            out.append((
                "error",
                f"{n} scores row(s) reference a partner_id not in partners. "
                f"SQL: SELECT partner_id, axis_id FROM scores WHERE "
                f"partner_id NOT IN (SELECT partner_id FROM partners);",
            ))
    return out


def _check_partners_have_funds(engine) -> list[tuple[Severity, str]]:
    """504: every partner's fund_id resolves."""
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(partners)
            .where(
                partners.c.fund_id.isnot(None),
                ~partners.c.fund_id.in_(select(funds.c.fund_id)),
            )
        ).scalar()
    if n:
        return [(
            "error",
            f"{n} partners reference a fund_id that doesn't exist. "
            f"SQL: SELECT partner_id, fund_id FROM partners WHERE fund_id "
            f"NOT IN (SELECT fund_id FROM funds);",
        )]
    return []


def _check_score_axis_ids_match_yaml(engine, ws) -> list[tuple[Severity, str]]:
    """508: every score row's axis_id is in axes.yaml."""
    valid = {ax["id"] for ax in (ws.axes or {}).get("axes", []) if ax.get("id")}
    if not valid:
        return [("warn", "axes.yaml empty -- skipped axis_id check")]
    with engine.begin() as conn:
        rows = conn.execute(
            select(scores.c.axis_id, func.count())
            .group_by(scores.c.axis_id)
        ).all()
    bad = [(aid, n) for aid, n in rows if aid not in valid]
    if not bad:
        return []
    detail = ", ".join(f"{aid!r} ({n} rows)" for aid, n in bad)
    return [(
        "error",
        f"{len(bad)} unknown axis_id(s) in scores: {detail}. "
        f"axes.yaml defines {sorted(valid)}. SQL: SELECT partner_id, axis_id "
        f"FROM scores WHERE axis_id NOT IN "
        f"({', '.join(repr(a) for a in sorted(valid))});",
    )]


def _check_duplicate_pending_suggestions(engine) -> list[tuple[Severity, str]]:
    """509: no duplicate unapproved suggestions per axis."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                axis_weight_suggestions.c.axis_id, func.count(),
            )
            .where(axis_weight_suggestions.c.approved.is_(None))
            .group_by(axis_weight_suggestions.c.axis_id)
        ).all()
    dupes = [(aid, n) for aid, n in rows if n > 1]
    if not dupes:
        return []
    detail = ", ".join(f"{aid}={n}" for aid, n in dupes)
    return [(
        "warn",
        f"duplicate pending suggestions: {detail}. monthly_learning_report "
        f"clears stale unapproved suggestions before generating new ones; if "
        f"this fires, two runs landed concurrently or the clear path is "
        f"broken.",
    )]


def _check_out_of_range_scores(engine) -> list[tuple[Severity, str]]:
    """510: composite + axis scores must lie in [0, 10]."""
    out: list[tuple[Severity, str]] = []
    with engine.begin() as conn:
        n_comp = conn.execute(
            select(func.count()).select_from(partner_score_summaries)
            .where(
                partner_score_summaries.c.composite_fit_score.isnot(None),
                (partner_score_summaries.c.composite_fit_score < 0)
                | (partner_score_summaries.c.composite_fit_score > 10),
            )
        ).scalar()
        if n_comp:
            out.append((
                "error",
                f"{n_comp} composite_fit_score(s) outside [0, 10]. "
                f"SQL: SELECT partner_id, composite_fit_score FROM "
                f"partner_score_summaries WHERE composite_fit_score < 0 OR "
                f"composite_fit_score > 10;",
            ))
        n_ax = conn.execute(
            select(func.count()).select_from(scores)
            .where(
                scores.c.score.isnot(None),
                (scores.c.score < 0) | (scores.c.score > 10),
            )
        ).scalar()
        if n_ax:
            out.append((
                "error",
                f"{n_ax} axis score(s) outside [0, 10]. "
                f"SQL: SELECT partner_id, axis_id, score FROM scores WHERE "
                f"score < 0 OR score > 10;",
            ))
    return out


def _check_future_dated_rows(engine) -> list[tuple[Severity, str]]:
    """511: no future-dated signals / deals / outcomes."""
    out: list[tuple[Severity, str]] = []
    today = date.today()
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(signals)
            .where(signals.c.quote_date > today)
        ).scalar()
        if n:
            out.append((
                "error",
                f"{n} signal(s) with quote_date in the future. "
                f"SQL: SELECT signal_id, quoted_text, quote_date FROM "
                f"signals WHERE quote_date > date('now');",
            ))
        n = conn.execute(
            select(func.count()).select_from(deal_attributions)
            .where(deal_attributions.c.announcement_date > today)
        ).scalar()
        if n:
            out.append((
                "error",
                f"{n} deal_attributions row(s) with announcement_date in the "
                f"future. SQL: SELECT deal_id, company, announcement_date "
                f"FROM deal_attributions WHERE announcement_date > date('now');",
            ))
        n = conn.execute(
            select(func.count()).select_from(outcomes)
            .where(outcomes.c.meeting_date > today)
        ).scalar()
        if n:
            out.append((
                "warn",
                f"{n} outcome row(s) with meeting_date in the future. "
                f"Likely legitimate (booked meeting), but flagged so the "
                f"operator can confirm.",
            ))
    return out


def _check_verified_quality_consistency(engine) -> list[tuple[Severity, str]]:
    """516/517: verified+quality flags consistent."""
    out: list[tuple[Severity, str]] = []
    with engine.begin() as conn:
        # 516: unverified with quality
        n = conn.execute(
            select(func.count()).select_from(signals)
            .where(
                signals.c.verified.is_(False),
                signals.c.signal_quality_score.isnot(None),
            )
        ).scalar()
        if n:
            out.append((
                "error",
                f"{n} unverified signal(s) carry a non-null "
                f"signal_quality_score. Stage 5 should null these on the "
                f"verified-False transition (Batch 11 #351/#352). SQL: "
                f"SELECT signal_id FROM signals WHERE verified=0 AND "
                f"signal_quality_score IS NOT NULL;",
            ))
        # 517: verified without quality
        n = conn.execute(
            select(func.count()).select_from(signals)
            .where(
                signals.c.verified.is_(True),
                signals.c.signal_quality_score.is_(None),
            )
        ).scalar()
        if n:
            out.append((
                "warn",
                f"{n} verified signal(s) lack a signal_quality_score. "
                f"Stage 5 should score every verified signal. Re-run "
                f"`scripts/05_verify_and_quality.py` (or `--force`).",
            ))
    return out


def _check_warm_path_not_ready(engine) -> list[tuple[Severity, str]]:
    """520: warm-path partners shouldn't be marked recommended_to_send.
    Stage 7 already routes them to warm_path_needed; this catches DB
    drift if a manual override slipped past."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(partners.c.partner_id, partners.c.name)
            .join(
                partner_score_summaries,
                partner_score_summaries.c.partner_id == partners.c.partner_id,
            )
            .where(
                partners.c.warm_path_available.is_(True),
                partner_score_summaries.c.recommended_to_send.is_(True),
            )
        ).all()
    if not rows:
        return []
    sample = ", ".join(f"{r.partner_id}" for r in rows[:5])
    return [(
        "warn",
        f"{len(rows)} partner(s) flagged warm_path_available=TRUE AND "
        f"recommended_to_send=TRUE (Stage 7 will route to warm_path_needed "
        f"on the next run, but the DB shows a contradiction). Sample: "
        f"{sample}",
    )]


def _check_recommended_has_draft(engine) -> list[tuple[Severity, str]]:
    """502: every recommended partner should have at least one is_recommended
    draft (otherwise Stage 7 hasn't generated for them yet)."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(partner_score_summaries.c.partner_id)
            .where(partner_score_summaries.c.recommended_to_send.is_(True))
        ).all()
        rec_pids = {r.partner_id for r in rows}
        if not rec_pids:
            return []
        draft_pids = {
            r.partner_id for r in conn.execute(
                select(email_drafts.c.partner_id)
                .where(email_drafts.c.is_recommended.is_(True))
                .distinct()
            )
        }
    missing = sorted(rec_pids - draft_pids)
    if not missing:
        return []
    sample = ", ".join(missing[:5])
    return [(
        "warn",
        f"{len(missing)} recommended partner(s) have NO is_recommended "
        f"email_drafts row. Run scripts/07_generate_emails.py. Sample: "
        f"{sample}",
    )]


def _check_orphan_outcomes(engine) -> list[tuple[Severity, str]]:
    """Batch 41 (#67): outcomes intentionally don't cascade-delete with
    partners (we keep history), but reports / CLIs that join through
    outcomes need to know about orphans so the join-failure mode is
    visible. Surfaced as warn, not error -- this is by-design state."""
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(outcomes)
            .where(~outcomes.c.partner_id.in_(select(partners.c.partner_id)))
        ).scalar()
    if not n:
        return []
    return [(
        "warn",
        f"{n} outcomes row(s) reference a partner_id not in partners "
        f"(intentional non-cascade -- history preserved across partner "
        f"removal). Reports must left-join or .get() defensively. "
        f"SQL: SELECT outcome_id, partner_id, outreach_status FROM "
        f"outcomes WHERE partner_id NOT IN "
        f"(SELECT partner_id FROM partners);",
    )]


def _check_orphan_snapshots(engine) -> list[tuple[Severity, str]]:
    """515: snapshots that no signal references and aren't recent."""
    with engine.begin() as conn:
        n = conn.execute(
            select(func.count()).select_from(source_snapshots)
            .where(~source_snapshots.c.snapshot_id.in_(
                select(signals.c.snapshot_id).where(
                    signals.c.snapshot_id.isnot(None)
                )
            ))
        ).scalar()
    if not n:
        return []
    # Orphan snapshots are normal -- Stage 2 fund pages don't necessarily
    # produce signals. Surface as info, not warn.
    return [(
        "info",
        f"{n} source_snapshots row(s) not referenced by any signal "
        f"(common for fund pages where Stage 2 enrichment didn't pivot to "
        f"a signal). No cleanup policy yet; track over time.",
    )]


def _check_placeholders_in_recommended_drafts(engine) -> list[tuple[Severity, str]]:
    """512/513: recommended drafts must not contain `{TOKEN}` placeholders."""
    out: list[tuple[Severity, str]] = []
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                email_drafts.c.partner_id, email_drafts.c.subject,
                email_drafts.c.body,
            ).where(email_drafts.c.is_recommended.is_(True))
        ).all()
    bad = [
        r for r in rows
        if _PLACEHOLDER_RE.search(r.subject or "")
        or _PLACEHOLDER_RE.search(r.body or "")
    ]
    if bad:
        sample = ", ".join(r.partner_id for r in bad[:3])
        out.append((
            "error",
            f"{len(bad)} recommended draft(s) contain {{TOKEN}} placeholders. "
            f"Sample: {sample}. Stage 7 hard gate should have caught these; "
            f"check generate_email.txt for unfilled prompt variables.",
        ))
    empty = [
        r for r in rows
        if not (r.subject or "").strip() or not (r.body or "").strip()
    ]
    if empty:
        sample = ", ".join(r.partner_id for r in empty[:3])
        out.append((
            "error",
            f"{len(empty)} recommended draft(s) have empty subject or body. "
            f"Sample: {sample}. SQL: SELECT partner_id, length(subject), "
            f"length(body) FROM email_drafts WHERE is_recommended=1 AND "
            f"(subject IS NULL OR body IS NULL);",
        ))
    return out


def _check_source_reachability(ws) -> list[tuple[Severity, str]]:
    """Batch 42 (#72): live-HEAD each configured source URL and report.
    Network failures = error; non-2xx = warn; 2xx = silent.

    Sources checked:
      - sources.yaml.public_lists[*].url
      - sources.yaml.funding_announcement_feeds[*].url
      - data/raw/partner_content_urls.csv source_url column
    Local `path:` sources just check file existence.
    """
    import asyncio
    import csv as _csv
    from core.http_client import HttpClient

    out: list[tuple[Severity, str]] = []
    targets: list[tuple[str, str]] = []  # (label, url-or-path)
    sources = ws.sources or {}
    for s in (sources.get("public_lists") or []):
        if s.get("url"):
            targets.append((f"public_lists:{s.get('name', '?')}", s["url"]))
        elif s.get("path"):
            p = (ws.path / s["path"]).resolve()
            if not p.exists():
                out.append((
                    "error",
                    f"public_lists:{s.get('name', '?')} path {p} not found",
                ))
    for f in (sources.get("funding_announcement_feeds") or []):
        if f.get("url"):
            targets.append((f"feed:{f.get('name', '?')}", f["url"]))
    csv_path = ws.path / "data" / "raw" / "partner_content_urls.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                url = (row.get("source_url") or "").strip()
                if url:
                    targets.append(
                        (f"partner_content:{row.get('partner_id')}", url),
                    )

    if not targets:
        return out

    client = HttpClient()

    async def _hit(label: str, url: str) -> tuple[Severity, str] | None:
        try:
            res = await client.fetch(url)
        except Exception as exc:  # noqa: BLE001
            return ("error", f"{label} {url!r}: fetch failed: {exc}")
        if res.status >= 500:
            return ("error", f"{label} {url!r}: HTTP {res.status}")
        if res.status >= 400:
            return ("warn", f"{label} {url!r}: HTTP {res.status}")
        return None

    async def _all():
        results: list[tuple[Severity, str] | None] = []
        for label, url in targets:
            results.append(await _hit(label, url))
        return results

    for r in asyncio.run(_all()):
        if r is not None:
            out.append(r)
    return out


def _check_draft_history_invariants(engine) -> list[tuple[Severity, str]]:
    """Slice 17: every (partner_id, version) live row is unique +
    superseded versions are strictly less than the live version. A
    drift here means Stage 7's supersede pattern was bypassed or a
    manual SQL edit went wrong."""
    out: list[tuple[Severity, str]] = []
    with engine.begin() as conn:
        # Two live rows for the same partner with is_recommended=TRUE
        # would confuse "latest rec" readers.
        rows = conn.execute(
            select(email_drafts.c.partner_id, func.count().label("n"))
            .where(
                email_drafts.c.superseded_at.is_(None),
                email_drafts.c.is_recommended.is_(True),
            )
            .group_by(email_drafts.c.partner_id)
            .having(func.count() > 1)
        ).all()
        if rows:
            sample = ", ".join(f"{r.partner_id}({r.n})" for r in rows[:5])
            out.append((
                "error",
                f"{len(rows)} partner(s) have >1 LIVE recommended draft "
                f"(should be exactly one per partner). Sample: {sample}",
            ))
        # Superseded rows that still claim is_recommended=TRUE.
        bad_rec = conn.execute(
            select(func.count())
            .select_from(email_drafts)
            .where(
                email_drafts.c.superseded_at.isnot(None),
                email_drafts.c.is_recommended.is_(True),
            )
        ).scalar() or 0
        if bad_rec:
            out.append((
                "warn",
                f"{bad_rec} superseded email_drafts row(s) still have "
                f"is_recommended=TRUE (Stage 7's supersede should clear it). "
                f"SQL: select draft_id from email_drafts where "
                f"superseded_at is not null and is_recommended=1",
            ))
        # Version ordering: every superseded row's version must be
        # less than every live row's version for the same partner.
        # Drop into raw SQL to avoid SQLAlchemy alias() boilerplate;
        # the doctor module already uses exec_driver_sql elsewhere.
        violations = next(iter(conn.exec_driver_sql(
            "SELECT COUNT(*) FROM email_drafts a "
            "JOIN email_drafts b ON a.partner_id = b.partner_id "
            "WHERE a.superseded_at IS NOT NULL "
            "  AND b.superseded_at IS NULL "
            "  AND a.version >= b.version"
        )))[0] or 0
        if violations:
            out.append((
                "error",
                f"{violations} email_drafts row pair(s) violate version "
                f"monotonicity (superseded version >= live version for the "
                f"same partner). SQL: "
                f"select a.draft_id, b.draft_id from email_drafts a "
                f"join email_drafts b on a.partner_id=b.partner_id "
                f"where a.superseded_at is not null and b.superseded_at is "
                f"null and a.version >= b.version",
            ))
    return out


def _check_sources_registry_consistency(engine) -> list[tuple[Severity, str]]:
    """Slice 18b: every source_snapshots.source_id should point at an
    existing sources row. NULL is permitted on legacy rows the m002
    backfill skipped (e.g. tables without source_url at all)."""
    with engine.begin() as conn:
        # Snapshots with source_id pointing at a non-existent source.
        orphan = conn.execute(
            select(func.count())
            .select_from(source_snapshots)
            .where(
                source_snapshots.c.source_id.isnot(None),
                ~source_snapshots.c.source_id.in_(
                    select(sources.c.source_id)
                ),
            )
        ).scalar() or 0
        if orphan:
            return [(
                "error",
                f"{orphan} source_snapshots row(s) have source_id pointing "
                f"at a non-existent sources row. SQL: "
                f"select snapshot_id, source_id from source_snapshots "
                f"where source_id is not null and source_id not in "
                f"(select source_id from sources)",
            )]
        # Snapshots with source_url but no source_id (post-Slice-18b
        # writes should populate it; legacy writes have m002 backfill).
        unstamped = conn.execute(
            select(func.count())
            .select_from(source_snapshots)
            .where(
                source_snapshots.c.source_id.is_(None),
                source_snapshots.c.source_url.isnot(None),
            )
        ).scalar() or 0
        if unstamped:
            return [(
                "warn",
                f"{unstamped} source_snapshots row(s) have source_url but "
                f"NULL source_id (m002 backfill should have populated "
                f"these; re-running get_engine on this DB should fix it). "
                f"SQL: select snapshot_id, source_url from source_snapshots "
                f"where source_id is null and source_url is not null",
            )]
    return []


def _check_override_events_consistency(engine) -> list[tuple[Severity, str]]:
    """Slice 18a: manual_override_events.partner_id should point at an
    existing partner. The FK declares ON DELETE CASCADE; an orphan
    here means the FK wasn't enforced on the original write (legacy
    DBs that pre-date the constraint)."""
    with engine.begin() as conn:
        orphan = conn.execute(
            select(func.count())
            .select_from(manual_override_events)
            .where(
                ~manual_override_events.c.partner_id.in_(
                    select(partners.c.partner_id)
                ),
            )
        ).scalar() or 0
        if orphan:
            return [(
                "error",
                f"{orphan} manual_override_events row(s) reference a "
                f"partner_id that no longer exists. SQL: "
                f"select event_id, partner_id from manual_override_events "
                f"where partner_id not in (select partner_id from partners)",
            )]
        # Kind must be one of score | rec | warm; action must be set | clear.
        bad_kind = conn.execute(
            select(func.count())
            .select_from(manual_override_events)
            .where(~manual_override_events.c.kind.in_(("score", "rec", "warm")))
        ).scalar() or 0
        bad_action = conn.execute(
            select(func.count())
            .select_from(manual_override_events)
            .where(~manual_override_events.c.action.in_(("set", "clear")))
        ).scalar() or 0
        if bad_kind or bad_action:
            return [(
                "error",
                f"manual_override_events has {bad_kind} unknown-kind + "
                f"{bad_action} unknown-action row(s). Kind must be "
                f"score|rec|warm; action must be set|clear.",
            )]
    return []


CHECKS = [
    _check_orphan_summaries,
    _check_partners_have_funds,
    _check_duplicate_pending_suggestions,
    _check_out_of_range_scores,
    _check_future_dated_rows,
    _check_verified_quality_consistency,
    _check_warm_path_not_ready,
    _check_recommended_has_draft,
    _check_orphan_snapshots,
    _check_orphan_outcomes,
    _check_placeholders_in_recommended_drafts,
    # Slice 17 / 18a / 18b invariants.
    _check_draft_history_invariants,
    _check_sources_registry_consistency,
    _check_override_events_consistency,
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DB invariant doctor. Read-only; never mutates."
    )
    add_workspace_arg(parser)
    parser.add_argument(
        "--json", action="store_true",
        help="Emit findings as JSON for programmatic consumption.",
    )
    parser.add_argument(
        "--check-source-reachability", action="store_true",
        help="Live-fetch each configured source (sources.yaml +"
             " partner_content_urls.csv) and report HTTP status. "
             "Opt-in because it makes real network requests. (Batch 42 #72)",
    )
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        # --json mode is for programmatic consumers; the banner would
        # pollute stdout. Human mode still gets the workspace/stage tag.
        print_banner(ws, stage="doctor")

    findings: list[tuple[Severity, str]] = []
    # axis-id check needs the workspace; others just the engine.
    findings.extend(_check_score_axis_ids_match_yaml(engine, ws))
    for fn in CHECKS:
        findings.extend(fn(engine))
    # Batch 42 (#72): optional live reachability check for configured
    # sources. Defers the actual fetch into a small helper so the
    # default `doctor.py` stays read-only/offline.
    if args.check_source_reachability:
        findings.extend(_check_source_reachability(ws))

    by_sev: dict[str, list[str]] = {"error": [], "warn": [], "info": []}
    for sev, msg in findings:
        by_sev.setdefault(sev, []).append(msg)

    if args.json:
        print(json.dumps({
            "errors": by_sev["error"],
            "warnings": by_sev["warn"],
            "infos": by_sev["info"],
        }, indent=2))
    else:
        print()
        if by_sev["error"]:
            print(f"== ERRORS ({len(by_sev['error'])}) ==")
            for m in by_sev["error"]:
                print(f"  - {m}")
        if by_sev["warn"]:
            print(f"== WARNINGS ({len(by_sev['warn'])}) ==")
            for m in by_sev["warn"]:
                print(f"  - {m}")
        if by_sev["info"]:
            print(f"== INFO ({len(by_sev['info'])}) ==")
            for m in by_sev["info"]:
                print(f"  - {m}")
        if not findings:
            print("doctor: all invariants clean")

    if by_sev["error"]:
        return 2
    if by_sev["warn"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
