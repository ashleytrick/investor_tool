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


@dataclass
class CachedArtifact:
    payload_json: str
    insufficient_evidence: bool
    model_used: str | None
    generated_at: datetime | None


def lookup(
    engine: Engine, *, partner_id: str, artifact_type: str,
    signal_set_hash: str,
) -> Optional[CachedArtifact]:
    """Return the cached row when (partner, type, hash) matches.
    None otherwise -- caller runs the builder and writes back."""
    with engine.begin() as conn:
        row = conn.execute(
            select(meeting_prep_artifacts).where(
                meeting_prep_artifacts.c.partner_id == partner_id,
                meeting_prep_artifacts.c.artifact_type == artifact_type,
                meeting_prep_artifacts.c.signal_set_hash == signal_set_hash,
            ).order_by(desc(meeting_prep_artifacts.c.artifact_id)).limit(1)
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
) -> None:
    """Append a new row. We don't update-in-place because the cache
    table is also an audit trail -- on a signal-set change the new
    row supersedes the old, and `lookup` returns the latest by
    artifact_id desc. Old rows stay for forensic comparison."""
    with engine.begin() as conn:
        conn.execute(
            meeting_prep_artifacts.insert().values(
                partner_id=partner_id,
                artifact_type=artifact_type,
                signal_set_hash=signal_set_hash,
                payload_json=payload_json,
                insufficient_evidence=insufficient_evidence,
                model_used=model_used,
                generated_at=datetime.now(timezone.utc),
            )
        )
