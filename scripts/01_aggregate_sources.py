"""Stage 1: aggregate the fund universe from free public sources.

Reads config/sources.yaml `public_lists`, parses each source, normalizes to the
canonical fund schema, dedupes by domain, and upserts into the `funds` table.
Enrichment (Stage 2) fills the remaining fund fields.

Run: uv run scripts/01_aggregate_sources.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.config_loader import add_workspace_arg, load_workspace
from core.banner import print_banner
from core.validate_config import preflight_or_exit
from core.db import funds, get_engine, upsert
from core.http_client import HttpClient
from core.ids import fund_id_for, normalize_domain
from core.runs import RunLogger

STAGE = "01_aggregate_sources"
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_csv(path: pathlib.Path) -> list[dict]:
    """Expect at least `name` and `domain` columns."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(_parse_csv_text(fh.read()))


def _parse_csv_text(text: str) -> list[dict]:
    """Parse CSV string with `name` + `domain` columns."""
    import io
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("name") or "").strip()
        domain = normalize_domain(row.get("domain") or "")
        if name and domain:
            rows.append({"name": name, "domain": domain})
    return rows


def _parse_markdown(text: str) -> list[dict]:
    """Parse a GitHub awesome-list style markdown: `[Name](https://url)`."""
    rows: list[dict] = []
    for name, url in _MD_LINK.findall(text):
        domain = normalize_domain(url)
        if name.strip() and domain:
            rows.append({"name": name.strip(), "domain": domain})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 1 source aggregation.")
    add_workspace_arg(parser)
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    preflight_or_exit(ws, stage=STAGE)
    print_banner(ws, stage=STAGE)
    engine = get_engine(ws.db_url)
    public_lists = ws.sources.get("public_lists") or []

    with RunLogger(engine, ws.name, STAGE) as run:
        seen: dict[str, dict] = {}  # domain -> fund row (first source wins)
        for src in public_lists:
            run.processed += 1
            name = src.get("name", "?")
            parser_kind = src.get("parser")
            try:
                if "path" in src:
                    src_path = (ws.path / src["path"]).resolve()
                    # Refuse paths that escape the workspace -- sources.yaml
                    # is operator-edited config, so a stray "../../etc/passwd"
                    # is a tenant-isolation hole.
                    ws_root = ws.path.resolve()
                    if not str(src_path).startswith(str(ws_root) + "/") \
                            and src_path != ws_root:
                        raise ValueError(
                            f"source path {src['path']!r} escapes workspace "
                            f"({ws_root}); refusing to read."
                        )
                    if not src_path.exists():
                        raise FileNotFoundError(src_path)
                    if parser_kind == "csv":
                        parsed = _parse_csv(src_path)
                    elif parser_kind == "markdown":
                        parsed = _parse_markdown(src_path.read_text(encoding="utf-8"))
                    else:
                        raise ValueError(f"unsupported parser: {parser_kind}")
                elif "url" in src:
                    # Live URL source. Fetch via http_client; parse based on
                    # parser_kind (markdown today; CSV at-URL trivially added).
                    client = HttpClient()
                    res = asyncio.run(client.fetch(src["url"]))
                    if res.status != 200 or not res.text:
                        raise RuntimeError(
                            f"URL fetch returned HTTP {res.status} / empty body"
                        )
                    if parser_kind == "markdown":
                        parsed = _parse_markdown(res.text)
                    elif parser_kind == "csv":
                        # CSV body served at a URL: parse from string.
                        parsed = list(_parse_csv_text(res.text))
                    else:
                        raise ValueError(
                            f"unsupported URL parser: {parser_kind!r}"
                        )
                else:
                    raise ValueError(
                        f"source {name!r} has neither `path` nor `url`"
                    )
            except Exception as exc:  # noqa: BLE001 - logged, run continues
                run.skipped += 1
                run.log_error(name, type(exc).__name__, str(exc))
                # Batch 36 (#7): sources.yaml entries can declare
                # `required: true`. A required source that fails to load
                # turns into a hard run failure -- partial ingestion is
                # only acceptable for sources the operator explicitly
                # marked optional. Default behavior (no `required` key)
                # is OPTIONAL, preserving back-compat.
                if src.get("required"):
                    run.failed += 1
                    print(
                        f"[stage 1] REQUIRED source {name!r} failed: {exc}"
                    )
                continue

            for row in parsed:
                seen.setdefault(row["domain"], row)
            run.succeeded += 1

        # Upsert deduped funds.
        with engine.begin() as conn:
            for domain, row in seen.items():
                upsert(conn, funds, ["fund_id"], {
                    "fund_id": fund_id_for(domain),
                    "name": row["name"],
                    "domain": domain,
                    "last_updated": _now(),
                })
        # Loud failure: sources were configured but produced nothing usable.
        # Quiet success of an empty pipeline is the old "no silent failures"
        # trap wearing a new jacket -- exit 2 so wrappers notice.
        # Batch 36 (#7): a required-source failure trips run.failed
        # inside the loop; surface it as exit 2 here too.
        if run.failed > 0:
            print(
                f"[stage 1] {run.failed} REQUIRED source(s) failed; "
                f"refusing to publish a partial fund universe."
            )
            return 2
        if not seen and run.processed > 0:
            print(
                f"[stage 1] FAIL: {run.processed} source(s) configured but "
                f"0 usable funds ingested. Check sources.yaml + recent errors."
            )
            run.note("zero funds ingested from configured sources")
            return 2
        print(f"[stage 1] {len(seen)} unique funds aggregated -> funds table")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
