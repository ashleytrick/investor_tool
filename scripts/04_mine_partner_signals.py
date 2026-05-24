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
import asyncio
import csv
import hashlib
import json
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from selectolax.parser import HTMLParser
from sqlalchemy import and_, select

from core.config_loader import add_workspace_arg
from core.stage_runner import stage_run
from core.db import funds, partners, signals, source_snapshots
from core.http_client import HttpClient
from core.llm.client import MODEL_BATCH
from schemas.partner_signals import PartnerSignalsOutput

STAGE = "04_mine_partner_signals"
PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "prompts" / "extract_partner_signals.txt"
)
PARTNER_CONTENT_URLS_PATH = "data/raw/partner_content_urls.csv"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


# Batch 36 (#12/#14): when a Stage 4 fetch fails OR returns non-200, we
# still want a source_snapshots row so the audit trail captures the
# attempt. http_status carries the real status (or -1 for transport
# failures); extracted_text stays NULL. final_url is the post-redirect
# URL when available.
def upsert_snapshot_failure(
    engine, source_url: str, *, http_status: int, final_url: str | None,
    note: str,
) -> int | None:
    chash = _content_hash(f"FAIL:{http_status}:{note}")
    with engine.begin() as conn:
        try:
            result = conn.execute(source_snapshots.insert().values(
                source_url=source_url,
                final_url=final_url,
                fetched_at=_now(),
                http_status=http_status,
                content_hash=chash,
                extracted_text=None,
                fetched_during_stage=STAGE,
            ))
            return int(result.inserted_primary_key[0])
        except Exception:  # noqa: BLE001 - UNIQUE collision on (url, hash)
            return None


def upsert_snapshot(engine, source_url: str, text: str,
                    *, final_url: str | None = None) -> int:
    """Return snapshot_id; create if (source_url, content_hash) not present."""
    chash = _content_hash(text)
    with engine.begin() as conn:
        existing = conn.execute(
            select(source_snapshots.c.snapshot_id).where(
                source_snapshots.c.source_url == source_url,
                source_snapshots.c.content_hash == chash,
            )
        ).first()
        if existing:
            return int(existing.snapshot_id)
        result = conn.execute(source_snapshots.insert().values(
            source_url=source_url,
            final_url=final_url,  # Batch 36 (#14): post-redirect URL
            fetched_at=_now(),
            http_status=200,
            content_hash=chash,
            extracted_text=text,
            fetched_during_stage=STAGE,
        ))
        return int(result.inserted_primary_key[0])


CSV_REQUIRED_HEADERS = {"partner_id", "source_type", "source_url"}


def _fetch_live_partner_content(
    ws, engine, run, known_partner_ids: set[str],
    *, strict_unknown_partners: bool = True,
) -> dict:
    """Read data/raw/partner_content_urls.csv (cols: partner_id, source_type,
    source_url), fetch each URL via http_client, and return a dict matching
    the partner_signals_seed.json shape so the rest of Stage 4 is identical.

    Batch 36 (#8/#10/#11/#12/#14):
    - Validates the CSV header against CSV_REQUIRED_HEADERS upfront and
      raises a clear error if missing.
    - Unknown partner_id rows are recorded in run_errors (and the run
      EXITS with run.failed when strict_unknown_partners=True).
    - Per-URL fetch failures land in run_errors AND a source_snapshots
      row with http_status set (or -1 for transport errors), instead of
      vanishing into stdout.
    - Successful fetches now record final_url (post-redirect).
    """
    csv_path = ws.path / PARTNER_CONTENT_URLS_PATH
    if not csv_path.exists():
        return {}
    client = HttpClient()
    out: dict[str, dict] = {}
    by_partner: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        # Batch 36 (#10): header validation. Reject upfront if a column
        # is missing -- otherwise we'd silently treat every row as
        # missing partner_id.
        missing = CSV_REQUIRED_HEADERS - set(reader.fieldnames or [])
        if missing:
            msg = (
                f"partner_content_urls.csv missing required column(s): "
                f"{sorted(missing)} (have: {reader.fieldnames})"
            )
            run.log_error(str(csv_path), "csv_schema", msg)
            run.failed += 1
            raise ValueError(msg)
        for row in reader:
            pid = (row.get("partner_id") or "").strip()
            url = (row.get("source_url") or "").strip()
            stype = (row.get("source_type") or "blog").strip()
            if not pid or not url:
                continue
            # Batch 36 (#11): unknown partner_id is a CSV error, not a
            # silent skip. Log + count as failure; the operator gets a
            # non-zero exit unless they pass --allow-unknown-partner-ids.
            if pid not in known_partner_ids:
                run.log_error(
                    pid, "unknown_partner_in_csv",
                    f"row references partner_id not in partners table; "
                    f"source_url={url}",
                )
                if strict_unknown_partners:
                    run.failed += 1
                continue
            by_partner[pid].append((stype, url))

    for pid, items in by_partner.items():
        sources: list[dict] = []
        for stype, url in items:
            try:
                res = asyncio.run(client.fetch(url))
            except Exception as exc:  # noqa: BLE001 - log + audit, move on
                print(f"[stage 4] {pid} fetch {url} failed: {exc}")
                # Batch 36 (#8/#12): record in run_errors AND snapshot
                # so the audit captures the failed attempt.
                run.log_error(
                    f"{pid}:{url}", "fetch_failed", str(exc),
                )
                upsert_snapshot_failure(
                    engine, url, http_status=-1, final_url=None,
                    note=str(exc),
                )
                run.failed += 1
                continue
            if res.status != 200 or not res.text:
                print(f"[stage 4] {pid} {url} -> HTTP {res.status}; skipping")
                run.log_error(
                    f"{pid}:{url}", "http_error",
                    f"HTTP {res.status} (final_url={res.final_url!r})",
                )
                upsert_snapshot_failure(
                    engine, url, http_status=res.status,
                    final_url=res.final_url,
                    note=f"HTTP {res.status}",
                )
                run.failed += 1
                continue
            text = HTMLParser(res.text).text(separator=" ", strip=True)
            if not text:
                run.log_error(
                    f"{pid}:{url}", "empty_body",
                    f"HTTP 200 but extracted text was empty "
                    f"(final_url={res.final_url!r})",
                )
                run.failed += 1
                continue
            sources.append({
                "source_type": stype,
                "source_url": url,
                "final_url": res.final_url,
                "quote_date": None,
                "text": text,
            })
        if sources:
            out[pid] = {"sources": sources}
            print(f"[stage 4] {pid}: {len(sources)} live content source(s) fetched")
    return out


def render_prompt(template: str, *, company: dict, partner_row, fund_name: str,
                  axes_block: str, content: str) -> str:
    c = company["company"]
    raise_ctx = company["raise_context"]
    return (
        template
        .replace("{COMPANY_NAME}", c["name"])
        .replace("{ROUND}", raise_ctx.get("round", ""))
        .replace("{AMOUNT}", raise_ctx.get("amount", ""))
        .replace("{PARTNER_NAME}", partner_row.name)
        .replace("{FUND_NAME}", fund_name)
        .replace("{AXES_BLOCK}", axes_block)
        .replace("{CONTENT}", content)
    )


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
            known_pids = {p.partner_id for p in partner_rows}
            try:
                fixture = _fetch_live_partner_content(
                    ws, engine, run, known_pids,
                    strict_unknown_partners=not args.allow_unknown_partner_ids,
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
                    url_to_snap: dict[str, int] = {}
                    content_parts: list[str] = []
                    for src in entry.get("sources", []):
                        sid = upsert_snapshot(
                            engine, src["source_url"], src["text"],
                            final_url=src.get("final_url"),
                        )
                        url_to_snap[src["source_url"]] = sid
                        content_parts.append(
                            f'--- {src["source_url"]} ({src["source_type"]}, '
                            f'{src.get("quote_date","?")}) ---\n{src["text"]}'
                        )
                    content = "\n\n".join(content_parts)

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

                    # Persist thesis signals (dedup on partner_id + source_url + quote).
                    inserted_here = 0
                    with engine.begin() as conn:
                        for s in output.signals:
                            url = str(s.source_url)
                            snap_id = url_to_snap.get(url)
                            exists = conn.execute(
                                select(signals.c.signal_id, signals.c.snapshot_id).where(and_(
                                    signals.c.partner_id == p.partner_id,
                                    signals.c.source_url == url,
                                    signals.c.quoted_text == s.quoted_text,
                                ))
                            ).first()
                            if exists:
                                # On dedup hit, REFRESH the metadata fields so a
                                # corrected LLM run actually updates axis_relevance
                                # / source_type / signal_direction / quote_date.
                                # Previously these were frozen at first insertion
                                # even when a later run produced better tags.
                                # verified + signal_quality_score are preserved
                                # (set by Stage 5; not for us to overwrite here).
                                update_values = {
                                    "source_type": s.source_type,
                                    "quote_date": s.quote_date,
                                    "axis_relevance": json.dumps(s.axis_relevance),
                                    "signal_direction": s.signal_direction,
                                }
                                if exists.snapshot_id is None and snap_id is not None:
                                    update_values["snapshot_id"] = snap_id
                                conn.execute(
                                    signals.update()
                                    .where(signals.c.signal_id == exists.signal_id)
                                    .values(**update_values)
                                )
                                continue
                            conn.execute(signals.insert().values(
                                partner_id=p.partner_id,
                                snapshot_id=url_to_snap.get(url),
                                source_type=s.source_type,
                                source_url=url,
                                quoted_text=s.quoted_text,
                                quote_date=s.quote_date,
                                axis_relevance=json.dumps(s.axis_relevance),
                                signal_direction=s.signal_direction,
                                verified=False,
                                captured_at=_now(),
                            ))
                            inserted_here += 1

                        # Persist reachability partial info on the partner row.
                        reach_payload = {
                            "reasoning": output.cold_reachability_reasoning,
                            "signals": [
                                {
                                    "evidence": e.evidence,
                                    "source_url": str(e.source_url),
                                    "direction": e.direction,
                                }
                                for e in output.reachability_signals
                            ],
                        }
                        conn.execute(
                            partners.update().where(partners.c.partner_id == p.partner_id)
                            .values(
                                cold_reachability_partial_score=output.cold_reachability_partial_score,
                                cold_reachability_partial_evidence=json.dumps(reach_payload),
                                last_updated=_now(),
                            )
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
