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

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.validate_config import preflight_or_exit
from core.db import funds, get_engine, partners, signals, source_snapshots
from core.http_client import HttpClient
from core.llm.client import MODEL_BATCH, LLMClient
from core.runs import RunLogger
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


def upsert_snapshot(engine, source_url: str, text: str) -> int:
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
            fetched_at=_now(),
            http_status=200,
            content_hash=chash,
            extracted_text=text,
            fetched_during_stage=STAGE,
        ))
        return int(result.inserted_primary_key[0])


def _fetch_live_partner_content(ws) -> dict:
    """Read data/raw/partner_content_urls.csv (cols: partner_id, source_type,
    source_url), fetch each URL via http_client, and return a dict matching
    the partner_signals_seed.json shape so the rest of Stage 4 is identical.

    No `_extraction` is set on live entries -- the LLM will produce signals
    in live mode. Stub mode would refuse upfront.
    """
    csv_path = ws.path / PARTNER_CONTENT_URLS_PATH
    if not csv_path.exists():
        return {}
    client = HttpClient()
    out: dict[str, dict] = {}
    by_partner: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("partner_id") or "").strip()
            url = (row.get("source_url") or "").strip()
            stype = (row.get("source_type") or "blog").strip()
            if pid and url:
                by_partner[pid].append((stype, url))

    for pid, items in by_partner.items():
        sources: list[dict] = []
        for stype, url in items:
            try:
                res = asyncio.run(client.fetch(url))
            except Exception as exc:  # noqa: BLE001 - log, move on
                print(f"[stage 4] {pid} fetch {url} failed: {exc}")
                continue
            if res.status != 200 or not res.text:
                print(f"[stage 4] {pid} {url} -> HTTP {res.status}; skipping")
                continue
            text = HTMLParser(res.text).text(separator=" ", strip=True)
            if not text:
                continue
            sources.append({
                "source_type": stype,
                "source_url": url,
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
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    preflight_or_exit(
        ws, stage=STAGE, require_anthropic=not args.fixtures,
    )
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    llm = LLMClient(workspace=ws)

    with engine.begin() as conn:
        partner_rows = list(conn.execute(
            select(partners.c.partner_id, partners.c.name, partners.c.fund_id)
        ))
        fund_name_by_id = {
            r.fund_id: r.name for r in conn.execute(select(funds.c.fund_id, funds.c.name))
        }

    template = PROMPT_PATH.read_text(encoding="utf-8")
    axes_block = build_axes_block(ws.axes)

    with RunLogger(engine, ws.name, STAGE) as run:
        run.attach_llm_usage(llm.usage)
        # Source content (inside RunLogger so failures land in `runs`).
        if args.fixtures:
            fixture = json.loads(
                (ws.fixtures_dir / "partner_signals_seed.json").read_text(encoding="utf-8")
            )
        else:
            fixture = _fetch_live_partner_content(ws)
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
                    msg = (
                        f"FAIL: {configured_rows} url(s) configured in "
                        f"{PARTNER_CONTENT_URLS_PATH} but 0 sources fetched. "
                        f"Check URL reachability."
                    )
                    print(f"[stage 4] {msg}")
                    run.note(msg)
                    run.failed = configured_rows
                    return 2
                print(
                    f"[stage 4] no live content fetched; populate "
                    f"{PARTNER_CONTENT_URLS_PATH} (cols: partner_id, "
                    f"source_type, source_url) or run with --fixtures."
                )
            if llm.stub and fixture:
                msg = (
                    f"REFUSED: {sum(len(v.get('sources', [])) for v in fixture.values())} "
                    f"live content sources fetched but llm is in stub mode. "
                    f"Set ANTHROPIC_API_KEY, or run with --fixtures."
                )
                print(f"[stage 4] {msg}")
                run.note(msg)
                run.failed = 1
                return 2
        partners_with_signals = 0
        total_signals = 0
        for p in partner_rows:
            entry = fixture.get(p.partner_id)
            if not entry:
                run.skipped += 1
                continue
            run.processed += 1
            try:
                # Snapshot each source; remember url -> snapshot_id.
                url_to_snap: dict[str, int] = {}
                content_parts: list[str] = []
                for src in entry.get("sources", []):
                    sid = upsert_snapshot(engine, src["source_url"], src["text"])
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
                run.succeeded += 1
                print(
                    f"[stage 4] {p.name}: {len(output.signals)} thesis signals "
                    f"({inserted_here} new), reach_partial="
                    f"{output.cold_reachability_partial_score}"
                )
            except Exception as exc:  # noqa: BLE001 - logged, continue
                run.failed += 1
                run.log_error(p.partner_id, type(exc).__name__, str(exc))

        print(
            f"[stage 4] {partners_with_signals} partners with >=1 thesis signal; "
            f"{total_signals} new signal rows"
        )
        print(f"[stage 4] llm stub mode: {llm.stub}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
