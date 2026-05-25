"""List partners or funds that lack critical fields.

Used to audit data completeness before a real outreach batch. The output
is plain text (or JSON via --json) so it can be piped into a CSV editor.

Examples:
  uv run scripts/list_missing_fields.py --workspace clients/foo --partners
  uv run scripts/list_missing_fields.py --workspace clients/foo --funds
  uv run scripts/list_missing_fields.py --workspace clients/foo --all --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from core.banner import print_banner
from core.config_loader import add_workspace_arg, load_workspace
from core.db import funds, get_engine, partners


def _missing_partners(engine) -> list[dict]:
    """A partner is incomplete when any of these are missing:
    name, title, fund_id, employment_status (or 'uncertain'), AND for
    outreach: email."""
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(select(partners)):
            missing: list[str] = []
            if not (r.name or "").strip():
                missing.append("name")
            if not (r.title or "").strip():
                missing.append("title")
            if not r.fund_id:
                missing.append("fund_id")
            if r.employment_status in (None, "", "uncertain"):
                missing.append("employment_status")
            if not (r.linkedin_url or "").strip():
                missing.append("linkedin_url")
            if not (r.email or "").strip():
                missing.append("email")
            if missing:
                out.append({
                    "partner_id": r.partner_id,
                    "name": r.name,
                    "fund_id": r.fund_id,
                    "missing": missing,
                })
    return out


def _missing_funds(engine) -> list[dict]:
    """A fund is incomplete when thesis / stage / check_size aren't set --
    Stage 6 round_fit silently scores those components 0 if so."""
    out: list[dict] = []
    with engine.begin() as conn:
        for r in conn.execute(select(funds)):
            missing: list[str] = []
            if not (r.stated_thesis or "").strip():
                missing.append("stated_thesis")
            if not (r.stated_stage_focus or "").strip():
                missing.append("stated_stage_focus")
            if not (r.check_size_range or "").strip():
                missing.append("check_size_range")
            if r.is_active is None:
                missing.append("is_active")
            if missing:
                out.append({
                    "fund_id": r.fund_id, "name": r.name,
                    "missing": missing,
                })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="List records missing critical fields.")
    add_workspace_arg(parser)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--partners", action="store_true")
    g.add_argument("--funds", action="store_true")
    g.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON for programmatic consumption.")
    args = parser.parse_args()

    ws = load_workspace(args.workspace)
    engine = get_engine(ws.db_url)
    if not args.json:
        print_banner(ws, stage="list_missing_fields")

    payload: dict = {}
    if args.partners or args.all:
        payload["partners"] = _missing_partners(engine)
    if args.funds or args.all:
        payload["funds"] = _missing_funds(engine)

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if "partners" in payload:
        print()
        print(f"== partners missing fields ({len(payload['partners'])}) ==")
        for r in payload["partners"]:
            print(
                f"  {r['partner_id']:50s} {r['name']!r:30s}  "
                f"missing: {r['missing']}"
            )
    if "funds" in payload:
        print()
        print(f"== funds missing fields ({len(payload['funds'])}) ==")
        for r in payload["funds"]:
            print(
                f"  {r['fund_id']:30s} {r['name']!r:30s}  "
                f"missing: {r['missing']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
