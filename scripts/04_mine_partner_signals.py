"""Stage 4: mine partner-level thesis + cold-reachability signals.

For each partner with available content (podcasts, blogs, social, etc.), runs
the partner-signal LLM (prompts/extract_partner_signals.txt) validated against
schemas/partner_signals.py. Persists thesis signals to the `signals` table with
verified=FALSE (Stage 5 verifies) and snapshot_id linked. Persists the
LLM-derived cold-reachability partial score and evidence to the `partners` row.

Stage 4 does NOT extract round_fit or lead_likelihood. Those are computed
deterministically in Stage 6 from observable facts.

Fixture mode (--fixtures): reads per-partner content from
data/fixtures/partner_signals_seed.json. Each entry carries an `_extraction`
dict used as the stub_response so the run is fully offline.

Run: uv run scripts/04_mine_partner_signals.py --workspace clients/test_workspace --fixtures
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import and_, select

from core.config_loader import add_workspace_arg
from core.db import funds, partners, signals, source_snapshots
from core.llm.client import MODEL_BATCH
from core.stage_runner import stage_run
from schemas.partner_signals import PartnerSignalsOutput

# Slice 18c: fetch + extract logic moved to core/stage4/. Re-exported
# below as module-level names so any external caller that imports
# from this script keeps working.
from core.stage4.fetch import (  # noqa: F401
    CSV_REQUIRED_HEADERS,
    PARTNER_CONTENT_URLS_PATH,
    STAGE,
    _fetch_live_partner_content,
    upsert_snapshot,
    upsert_snapshot_failure,
)
from core.stage4.extract import render_prompt  # noqa: F401

PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "prompts" / "extract_partner_signals.txt"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_axes_block(axes_cfg: dict) -> str:
    """Render axes.yaml as the AXES_BLOCK string injected into the prompt."""
    lines: list[str] = []
    for ax in axes_cfg.get("axes", []):
        lines.append(f'- {ax["id"]} "{ax["name"]}": {ax.get("description","")}')
        if ax.get("positive_signals"):
            lines.append(f'  Positive: {", ".join(ax["positive_signals"])}')
        if ax.get("negative_signals"):
            lines.append(f'  Negative: {", ".join(ax["negative_signals"])}')
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 4 partner-signal mining.")
    add_workspace_arg(parser)
    parser.add_argument("--fixtures", action="store_true",
                        help="Read per-partner content from "
                             "data/fixtures/partner_signals_seed.json")
    parser.add_argument(
        "--allow-unknown-partner-ids", action="store_true",
        help="Don't fail when partner_content_urls.csv references "
             "partner_ids that aren't in the partners table; just skip "
             "those rows (Batch 36 #11). Useful when the CSV is being "
             "drafted incrementally.",
    )
    parser.add_argument(
        "--allow-incomplete-content-csv", action="store_true",
        help="Permit clearing stale reachability partials for partners "
             "MISSING from partner_content_urls.csv (Batch 36 #9). By "
             "default Stage 4 refuses to clear partials when the CSV is "
             "incomplete (covers fewer than 50%% of known partners), to "
             "avoid losing all reachability when the operator only "
             "drafted a subset.",
    )
    args = parser.parse_args()
    # Refactor sweep: stage_run() boilerplate collapse.
    with stage_run(
        args, stage=STAGE,
        require_anthropic=not args.fixtures,
    ) as ctx:
        ws, engine, run, llm = ctx.ws, ctx.engine, ctx.run, ctx.llm

        with engine.begin() as conn:
            # Include reachability fields so the "no fresh content"
            # branch can decide whether to clear stale partials
            # (Batch 11 #348).
            partner_rows = list(conn.execute(
                select(
                    partners.c.partner_id, partners.c.name, partners.c.fund_id,
                    partners.c.cold_reachability_partial_score,
                    partners.c.cold_reachability_partial_evidence,
                )
            ))
            fund_name_by_id = {
                r.fund_id: r.name for r in conn.execute(select(funds.c.fund_id, funds.c.name))
            }

        template = PROMPT_PATH.read_text(encoding="utf-8")
        axes_block = build_axes_block(ws.axes)
        # Source content (inside RunLogger so failures land in `runs`).
        if args.fixtures:
            fixture = json.loads(
                (ws.fixtures_dir / "partner_signals_seed.json").read_text(encoding="utf-8")
            )
        else:
            # WorkspacePolicy routes the CSV strictness decision (item 10).
            from core.workspace_policy import WorkspacePolicy
            policy = WorkspacePolicy.from_workspace_and_args(ws, args)
            known_pids = {p.partner_id for p in partner_rows}
            try:
                fixture = _fetch_live_partner_content(
                    ws, engine, run, known_pids,
                    strict_unknown_partners=not policy.allow_unknown_partner_ids,
                )
            except ValueError as exc:
                # CSV schema validation refusal -- already logged.
                ctx.refuse(f"CSV schema invalid: {exc}")
                print(f"[stage 4] REFUSED: {exc}")
                return ctx.exit_code
            csv_path = ws.path / PARTNER_CONTENT_URLS_PATH
            configured_rows = 0
            if csv_path.exists():
                with csv_path.open(encoding="utf-8") as fh:
                    configured_rows = sum(
                        1 for i, line in enumerate(fh)
                        if i > 0 and line.strip()
                    )
            if not fixture:
                if configured_rows > 0:
                    ctx.refuse(
                        f"FAIL: {configured_rows} url(s) configured in "
                        f"{PARTNER_CONTENT_URLS_PATH} but 0 sources "
                        f"fetched. Check URL reachability."
                    )
                    # Preserve historical run.failed = configured_rows so
                    # cron audits show the actual URL count.
                    run.failed = configured_rows
                    print(f"[stage 4] REFUSED: see runs.error_summary")
                    return ctx.exit_code
                print(
                    f"[stage 4] no live content fetched; populate "
                    f"{PARTNER_CONTENT_URLS_PATH} (cols: partner_id, "
                    f"source_type, source_url) or run with --fixtures."
                )
            if llm.stub and fixture:
                ctx.refuse(
                    f"REFUSED: {sum(len(v.get('sources', [])) for v in fixture.values())} "
                    f"live content sources fetched but llm is in stub mode. "
                    f"Set ANTHROPIC_API_KEY, or run with --fixtures."
                )
                print(f"[stage 4] REFUSED: see runs.error_summary")
                return ctx.exit_code
        # Batch 36 (#9): safety gate on stale-reachability clearing.
        # Batch 11 added the clear, but if the operator runs Stage 4
        # with a partial CSV (e.g. only the 5 most recent partners), we
        # would wipe reachability for every OTHER partner -- catastrophic
        # data loss disguised as a routine re-run. Skip the clear when
        # coverage is below 50%, unless the operator explicitly opts in.
        coverage = len(fixture) / max(1, len(partner_rows))
        clear_stale_partials = (
            coverage >= 0.5 or args.allow_incomplete_content_csv
            or args.fixtures
        )
        if not clear_stale_partials and not args.fixtures:
            msg = (
                f"content coverage is {coverage:.0%} of known partners "
                f"({len(fixture)}/{len(partner_rows)}); REFUSING to clear "
                f"stale reachability partials. Pass "
                f"--allow-incomplete-content-csv to override, OR add the "
                f"missing partners to data/raw/partner_content_urls.csv."
            )
            print(f"[stage 4] {msg}")
            run.note(msg)
        partners_with_signals = 0
        total_signals = 0
        for p in partner_rows:
            with run.attempt():
                entry = fixture.get(p.partner_id)
                if not entry:
                    # Batch 11 (#348): a partner with no fresh content this run
                    # used to keep their stale cold_reachability_partial_score
                    # forever, so Stage 6's send_now_priority kept boosting a
                    # partner whose evidence was N runs old. Clear the partial
                    # score + evidence so Stage 6 treats reachability as unknown
                    # (post-Batch 5: unknown contributes 0, not 5).
                    # Batch 36 (#9): gated by `clear_stale_partials` so a
                    # partial CSV can't accidentally wipe reachability for
                    # every absent partner.
                    if (
                        clear_stale_partials
                        and (
                            p.cold_reachability_partial_score is not None
                            or p.cold_reachability_partial_evidence is not None
                        )
                    ):
                        with engine.begin() as conn:
                            conn.execute(
                                partners.update()
                                .where(partners.c.partner_id == p.partner_id)
                                .values(
                                    cold_reachability_partial_score=None,
                                    cold_reachability_partial_evidence=None,
                                    last_updated=_now(),
                                )
                            )
                        run.note(
                            f"cleared stale reachability partial for "
                            f"{p.partner_id} (no fresh content)"
                        )
                    run.skip()
                    continue
                try:
                    # Snapshot each source; remember url -> snapshot_id.
                    # `final_url` is populated by the live fetch path
                    # (Batch 36 #14) and absent in fixture mode; pass it
                    # through so source_snapshots.final_url captures the
                    # post-redirect URL on successful snapshots, not just
                    # on the failure path.
                    # Snapshot each source + remember url -> snapshot_id
                    # for downstream dedup. Content assembly + signal
                    # shaping + reachability payload live in
                    # core/partner_evidence.py (Refactor item 7/12).
                    from core.partner_evidence import (
                        build_reachability_payload,
                        format_content_block,
                        partner_reachability_values,
                        signal_insert_values,
                        signal_update_values,
                    )
                    url_to_snap: dict[str, int] = {}
                    for src in entry.get("sources", []):
                        sid = upsert_snapshot(
                            engine, src["source_url"], src["text"],
                            final_url=src.get("final_url"),
                        )
                        url_to_snap[src["source_url"]] = sid
                    content = format_content_block(entry.get("sources", []))

                    prompt = render_prompt(
                        template,
                        company=ws.company,
                        partner_row=p,
                        fund_name=fund_name_by_id.get(p.fund_id, "?"),
                        axes_block=axes_block,
                        content=content,
                    )
                    output: PartnerSignalsOutput = llm.complete_json(
                        prompt=prompt,
                        schema=PartnerSignalsOutput,
                        model=MODEL_BATCH,
                        stub_response=entry.get("_extraction"),
                    )

                    # Persist thesis signals (dedup on partner_id +
                    # source_url + quote).
                    inserted_here = 0
                    now = _now()
                    with engine.begin() as conn:
                        for s in output.signals:
                            url = str(s.source_url)
                            snap_id = url_to_snap.get(url)
                            exists = conn.execute(
                                select(
                                    signals.c.signal_id,
                                    signals.c.snapshot_id,
                                ).where(and_(
                                    signals.c.partner_id == p.partner_id,
                                    signals.c.source_url == url,
                                    signals.c.quoted_text == s.quoted_text,
                                ))
                            ).first()
                            if exists:
                                conn.execute(
                                    signals.update()
                                    .where(signals.c.signal_id == exists.signal_id)
                                    .values(**signal_update_values(
                                        existing_snapshot_id=exists.snapshot_id,
                                        new_signal=s,
                                        new_snapshot_id=snap_id,
                                    ))
                                )
                                continue
                            conn.execute(signals.insert().values(
                                **signal_insert_values(
                                    partner_id=p.partner_id,
                                    signal=s,
                                    snapshot_id=snap_id,
                                    captured_at=now,
                                )
                            ))
                            inserted_here += 1

                        # Persist reachability partial info on the
                        # partner row.
                        reach_payload = build_reachability_payload(output)
                        conn.execute(
                            partners.update()
                            .where(partners.c.partner_id == p.partner_id)
                            .values(**partner_reachability_values(
                                score=output.cold_reachability_partial_score,
                                payload=reach_payload,
                                now=now,
                            ))
                        )

                    total_signals += inserted_here
                    if output.signals:
                        partners_with_signals += 1
                    print(
                        f"[stage 4] {p.name}: {len(output.signals)} thesis signals "
                        f"({inserted_here} new), reach_partial="
                        f"{output.cold_reachability_partial_score}"
                    )
                except Exception as exc:  # noqa: BLE001 - logged, continue
                    run.fail(p.partner_id, type(exc).__name__, str(exc))

        print(
            f"[stage 4] {partners_with_signals} partners with >=1 thesis signal; "
            f"{total_signals} new signal rows"
        )
        print(f"[stage 4] llm stub mode: {llm.stub}")
    return ctx.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
