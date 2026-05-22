"""Stage 1: aggregate the fund universe from free public sources.

Reads config/sources.yaml `public_lists`, parses each source, normalizes to the
canonical fund schema, dedupes by domain, and upserts into the `funds` table.
Enrichment (Stage 2) fills the remaining fund fields.

Run: uv run scripts/01_aggregate_sources.py --workspace clients/test_workspace
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine, upsert
from core.ids import fund_id_for, normalize_domain
from core.runs import RunLogger

STAGE = "01_aggregate_sources"
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_csv(path: pathlib.Path) -> list[dict]:
    """Expect at least `name` and `domain` columns."""
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
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
                    src_path = ws.path / src["path"]
                    if not src_path.exists():
                        raise FileNotFoundError(src_path)
                    if parser_kind == "csv":
                        parsed = _parse_csv(src_path)
                    elif parser_kind == "markdown":
                        parsed = _parse_markdown(src_path.read_text(encoding="utf-8"))
                    else:
                        raise ValueError(f"unsupported parser: {parser_kind}")
                else:
                    # URL sources require network; skip cleanly when unreachable.
                    raise NotImplementedError(
                        f"URL source '{name}' skipped (no network in fixture run)"
                    )
            except Exception as exc:  # noqa: BLE001 - logged, run continues
                run.skipped += 1
                run.log_error(name, type(exc).__name__, str(exc))
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
        print(f"[stage 1] {len(seen)} unique funds aggregated -> funds table")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
