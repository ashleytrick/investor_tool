"""Stage 4 prompt rendering + extraction helpers (Slice 18c).

  - render_prompt(template, *, company, partner_row, fund_name,
                  axes_block, content) -> str
        Fill the partner-signal LLM prompt template with the
        per-partner context the operator's signals run needs.

Lifted verbatim from scripts/04_mine_partner_signals.py; signature
unchanged so any external caller importing it from the script keeps
working through the back-compat re-export there.

The LLM call itself (`llm.complete_json(...)`) stays inside the
Stage 4 script's main loop -- it's tightly coupled to per-partner
context that doesn't compose into a pure function without dragging
the whole partner row + signals + reachability pipeline along.
This module owns the pure template-filling step; the script owns the
LLM dispatch + per-row error handling.
"""
from __future__ import annotations


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
