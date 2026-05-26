"""Cache layer for meeting-prep artifacts.

Build Session 12's hard rule: re-running prep_brief.py against a
partner whose signal set hasn't changed must produce ZERO LLM calls.
The cache table holds the validated JSON payload keyed on
(partner_id, artifact_type, signal_set_hash); the hash is a sorted
sha256 of the verified, quality>=2 signal_ids the builder saw.

Same content-hash idiom source_snapshots already uses for fetched
HTML -- if the inputs match byte-for-byte, the output is trusted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine

from core.db import meeting_prep_artifacts


def hash_signal_set(signal_ids: Iterable[int]) -> str:
    """Stable hash for a set of signal_ids. Sorted before hashing so
    the order the caller iterates partners doesn't affect cache hits.
    Empty input hashes to a distinct value too (not the empty string)
    so a partner with zero signals still has a cache key."""
    ordered = sorted(int(s) for s in signal_ids)
    payload = json.dumps(ordered, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_company_profile(company_cfg: dict | None) -> str:
    """Stable hash of the dossier-relevant company.yaml fields.

    Why this is its own hash: the dossier prompt threads
    problem/solution/differentiators/desired_traits/excluded_sectors
    directly into the LLM input. When the founder updates those via
    the wizard, every cached dossier should regenerate, even on
    partners whose verified signal set is unchanged. Hashing the
    SUBSET of fields the prompt reads (rather than the whole YAML
    blob) prevents an unrelated edit -- e.g. `tone` -- from busting
    every dossier in the workspace.
    """
    c = (company_cfg or {}).get("company") or {}
    rc = (company_cfg or {}).get("raise_context") or {}
    payload = {
        "name": _str(c, "name"),
        "one_liner": _str(c, "one_liner"),
        "problem": _str(c, "problem"),
        "solution": _str(c, "solution"),
        "differentiators": _str(c, "differentiators"),
        "why_now": _str(c, "why_now"),
        "traction": _str(c, "traction"),
        "desired_traits": _list(c, "desired_traits"),
        "excluded_sectors": _list(c, "excluded_sectors"),
        "round_amount_usd": c.get("round_amount_usd"),
        "round_instrument": _str(c, "round_instrument"),
        "round_close_target": _str(c, "round_close_target"),
        # raise_context fallbacks the prompt reads when the flat
        # fields are empty.
        "rc_amount": _str(rc, "amount"),
        "rc_instrument": _str(rc, "instrument"),
        "rc_timing": _str(rc, "timing"),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _str(d: dict, key: str) -> str:
    v = d.get(key)
    return v if isinstance(v, str) else ""


def _list(d: dict, key: str) -> list[str]:
    v = d.get(key)
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


@dataclass
class CachedArtifact:
    payload_json: str
    insufficient_evidence: bool
    model_used: str | None
    generated_at: datetime | None


def lookup(
    engine: Engine, *, partner_id: str, artifact_type: str,
    signal_set_hash: str,
    company_profile_hash: str | None = None,
    live_research_hash: str | None = None,
    style_sample_hash: str | None = None,
) -> Optional[CachedArtifact]:
    """Return the cached row when ALL hash keys match.

    Backward-compat: callers that don't pass the extended hashes
    (objection_map + framing_brief from earlier sessions) only need
    the signal_set match. The dossier builder threads all four
    hashes through so a company.yaml edit or a --live-research run
    invalidates the cache.
    """
    with engine.begin() as conn:
        q = select(meeting_prep_artifacts).where(
            meeting_prep_artifacts.c.partner_id == partner_id,
            meeting_prep_artifacts.c.artifact_type == artifact_type,
            meeting_prep_artifacts.c.signal_set_hash == signal_set_hash,
        )
        if company_profile_hash is not None:
            q = q.where(
                meeting_prep_artifacts.c.company_profile_hash
                == company_profile_hash
            )
        if live_research_hash is not None:
            q = q.where(
                meeting_prep_artifacts.c.live_research_hash
                == live_research_hash
            )
        if style_sample_hash is not None:
            q = q.where(
                meeting_prep_artifacts.c.style_sample_hash
                == style_sample_hash
            )
        row = conn.execute(
            q.order_by(desc(meeting_prep_artifacts.c.artifact_id)).limit(1)
        ).first()
    if row is None:
        return None
    return CachedArtifact(
        payload_json=row.payload_json,
        insufficient_evidence=bool(row.insufficient_evidence),
        model_used=row.model_used,
        generated_at=row.generated_at,
    )


def write(
    engine: Engine, *, partner_id: str, artifact_type: str,
    signal_set_hash: str, payload_json: str,
    insufficient_evidence: bool, model_used: str | None,
    company_profile_hash: str | None = None,
    live_research_hash: str | None = None,
    style_sample_hash: str | None = None,
    content_markdown: str | None = None,
    source_summary_json: str | None = None,
    created_by: str | None = None,
) -> int:
    """Append a new row and return the new artifact_id.

    We don't update-in-place because the cache table is also an
    audit trail -- on a hash-key change the new row supersedes the
    old, and `lookup` returns the latest by artifact_id desc. Old
    rows stay for forensic comparison.

    Build Session 14: returns the new artifact_id so callers can
    stamp it on the resolving review_items row.
    """
    with engine.begin() as conn:
        result = conn.execute(
            meeting_prep_artifacts.insert().values(
                partner_id=partner_id,
                artifact_type=artifact_type,
                signal_set_hash=signal_set_hash,
                company_profile_hash=company_profile_hash,
                live_research_hash=live_research_hash,
                style_sample_hash=style_sample_hash,
                payload_json=payload_json,
                content_markdown=content_markdown,
                source_summary_json=source_summary_json,
                insufficient_evidence=insufficient_evidence,
                model_used=model_used,
                created_by=created_by,
                generated_at=datetime.now(timezone.utc),
            )
        )
        return int(result.inserted_primary_key[0])
