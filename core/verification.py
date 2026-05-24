"""Signal verification gauntlet.

For each unverified signal:
  1. Try a fast live fetch of source_url. On 200, substring-match the quoted
     text (whitespace-normalized, case-insensitive) against the page text.
  2. If live fails or quote not found, fall back to the trusted source_snapshot
     captured during Stage 4 extraction (referenced by signals.snapshot_id).
     The snapshot's content_hash is the hash of the text the LLM saw at
     extraction time, so substring-matching the snapshot is the brief's
     "snapshot fallback with content_hash trust" rule.
  3. Otherwise verified=False with a method that explains why.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.db import source_snapshots

VERIFY_TIMEOUT_S = 5.0
_WS = re.compile(r"\s+")


@dataclass
class VerificationResult:
    verified: bool
    verification_method: str
    verification_error: Optional[str] = None


def _normalize(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def substring_match(quote: str, text: str) -> bool:
    return _normalize(quote) in _normalize(text)


async def _fetch_short(url: str) -> Optional[str]:
    """Single-attempt fetch with a short timeout. Returns None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_S, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "investor-outreach/verifier"})
            if r.status_code == 200 and r.text:
                return HTMLParser(r.text).text(separator=" ", strip=True)
    except Exception:  # noqa: BLE001 - verification deliberately tolerates failure
        return None
    return None


def verify_signal(
    engine: Engine,
    source_url: str,
    quoted_text: str,
    snapshot_id: Optional[int],
    *,
    offline: bool = False,
) -> VerificationResult:
    """Apply the live-then-snapshot gauntlet to one signal.

    Batch 28 (#354): `offline=True` SKIPS the live fetch and verifies
    only against the captured snapshot. Useful when (a) the operator
    is on a flaky network, (b) the source sites all rate-limit them,
    or (c) running Stage 5 inside a sandbox without outbound network.
    """
    # 1. Live fetch (skipped in offline mode).
    live_text: Optional[str] = None
    if not offline:
        live_text = asyncio.run(_fetch_short(source_url))
        if live_text and substring_match(quoted_text, live_text):
            return VerificationResult(True, "live_match")

    # 2. Snapshot fallback — trusted only when the signal points to a snapshot
    # captured during extraction.
    snap_text: Optional[str] = None
    if snapshot_id is not None:
        with engine.begin() as conn:
            snap_text = conn.execute(
                select(source_snapshots.c.extracted_text)
                .where(source_snapshots.c.snapshot_id == snapshot_id)
            ).scalar()
    if snap_text and substring_match(quoted_text, snap_text):
        return VerificationResult(True, "snapshot_fallback")

    # 3. Failure cases.
    if offline:
        # In offline mode "no snapshot" is its own distinguishable reason
        # so the operator knows re-running with network would help.
        if snap_text is None:
            return VerificationResult(
                False, "no_snapshot_offline",
                "offline mode: no snapshot for this signal",
            )
        return VerificationResult(
            False, "quote_not_in_snapshot",
            "offline mode: quote not found in snapshot",
        )
    if live_text is None and snap_text is None:
        return VerificationResult(False, "url_failed",
                                  "live fetch failed and no usable snapshot")
    return VerificationResult(False, "quote_not_found",
                              "quote not found in live page or snapshot")
