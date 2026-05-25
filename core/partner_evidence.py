"""Stage 4 partner-evidence helpers (Refactor item 7 / 12).

Pure helpers Stage 4 uses to turn LLM-extracted partner signals into
DB rows. Split out of scripts/04_mine_partner_signals.py so the
dedup-on-rerun + reachability-payload logic is unit-testable without
a workspace fixture.

  - format_content_block(sources) -> str
      Assembles the LLM prompt's CONTENT section: one delimited
      block per source with `--- url (type, date) ---\\ntext`.

  - signal_update_values(existing_snapshot_id, new_signal,
                         new_snapshot_id) -> dict
      On a dedup hit (signals row already exists for this
      partner+url+quote), return the column->value dict to update.
      Refreshes metadata fields (axis_relevance / source_type /
      signal_direction / quote_date) so a corrected LLM run actually
      updates the tags; verified + signal_quality_score are
      preserved (set by Stage 5). snapshot_id only fills if the
      existing row had none AND a new one is available.

  - signal_insert_values(partner_id, signal, snapshot_id,
                         captured_at) -> dict
      Shape an LLM-extracted signal into a fresh signals row.

  - build_reachability_payload(output) -> dict
      Build the JSON blob (`{reasoning, signals}`) stored in
      partners.cold_reachability_partial_evidence.

  - partner_reachability_values(score, payload_json, now) -> dict
      Column->value dict for the partner row update.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def format_content_block(sources: list[dict]) -> str:
    """Assemble the LLM prompt's CONTENT section. `sources` is a list
    of dicts with keys: source_url, source_type, text, and an
    optional quote_date (defaults to '?' when missing)."""
    parts = [
        f'--- {s["source_url"]} ({s["source_type"]}, '
        f'{s.get("quote_date","?")}) ---\n{s["text"]}'
        for s in sources
    ]
    return "\n\n".join(parts)


def signal_update_values(
    *,
    existing_snapshot_id: int | None,
    new_signal: Any,
    new_snapshot_id: int | None,
) -> dict:
    """Build the values dict for a signals.update() when a dedup hit
    fires (the partner+url+quote combination already exists).

    Metadata fields (axis_relevance / source_type / signal_direction /
    quote_date) are always refreshed so a corrected LLM run actually
    updates the tags. verified + signal_quality_score are NOT touched
    -- Stage 5 owns those, and overwriting them here would silently
    unverify a previously-verified signal.

    snapshot_id only fills when the existing row had none AND a new
    snapshot exists -- a re-run with a fresh snapshot should backfill
    the link, but we don't churn snapshot_id on every dedup hit.
    """
    out = {
        "source_type": new_signal.source_type,
        "quote_date": new_signal.quote_date,
        "axis_relevance": json.dumps(new_signal.axis_relevance),
        "signal_direction": new_signal.signal_direction,
    }
    if existing_snapshot_id is None and new_snapshot_id is not None:
        out["snapshot_id"] = new_snapshot_id
    return out


def signal_insert_values(
    *,
    partner_id: str,
    signal: Any,
    snapshot_id: int | None,
    captured_at: datetime,
) -> dict:
    """Shape an LLM-extracted signal into a signals.insert() values
    dict. verified defaults to False; Stage 5's gauntlet flips it on
    after verifying the quote against the snapshot."""
    return {
        "partner_id": partner_id,
        "snapshot_id": snapshot_id,
        "source_type": signal.source_type,
        "source_url": str(signal.source_url),
        "quoted_text": signal.quoted_text,
        "quote_date": signal.quote_date,
        "axis_relevance": json.dumps(signal.axis_relevance),
        "signal_direction": signal.signal_direction,
        "verified": False,
        "captured_at": captured_at,
    }


def build_reachability_payload(output: Any) -> dict:
    """Build the JSON-serializable evidence blob from a
    PartnerSignalsOutput. The shape is intentionally small (just
    `reasoning` + a list of per-signal evidence dicts) because Stage 6's
    cold_reachability computation only consumes the score; the
    evidence is for operator audit."""
    return {
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


def partner_reachability_values(
    *,
    score: float | None,
    payload: dict,
    now: datetime,
) -> dict:
    """Column->value dict for the partners.update() that persists the
    Stage 4 reachability partial. Stage 6 reads
    cold_reachability_partial_score; the evidence column carries the
    JSON-serialized payload for audit."""
    return {
        "cold_reachability_partial_score": score,
        "cold_reachability_partial_evidence": json.dumps(payload),
        "last_updated": now,
    }
