"""One-line startup banner so each script surfaces key state up front
instead of buried in stage chatter."""
from __future__ import annotations

from core.config_loader import Workspace


def print_banner(ws: Workspace, *, stage: str | None = None) -> None:
    llm_mode = "stub" if not ws.env("ANTHROPIC_API_KEY") else "live"
    attio_mode = "off" if not (ws.attio or {}) else (
        "ready" if ws.env("ATTIO_API_KEY") else "configured-but-no-key"
    )
    parts = [
        f"workspace={ws.name}",
        f"llm={llm_mode}",
        f"attio={attio_mode}",
    ]
    if stage:
        parts.insert(0, f"stage={stage}")
    print(f"[{' | '.join(parts)}]")
