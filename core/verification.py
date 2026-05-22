"""Signal verification gauntlet.

SESSION 1 STUB. The real implementation (Session 5) does live URL resolution,
whitespace-normalized substring quote matching, and snapshot fallback. Until
then this returns a canned pass so the vertical slice runs end to end.
"""
from __future__ import annotations

from dataclasses import dataclass

STUB = True


@dataclass
class VerificationResult:
    verified: bool
    verification_method: str
    verification_error: str | None = None


def verify_signal(source_url: str, quoted_text: str) -> VerificationResult:
    """STUB: assume the signal verifies. Replace in Session 5."""
    return VerificationResult(verified=True, verification_method="stub_pass")
