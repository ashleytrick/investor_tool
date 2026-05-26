"""Shared FastAPI dependencies + helpers.

Hoisted out of `web/api.py` so the per-feature routers under
`web/routers/` can import them without creating a circular import
back to the main app module. The behavior is unchanged -- this file
is purely a relocation.
"""
from __future__ import annotations

import hmac
import os
import pathlib
import subprocess
import sys

from fastapi import HTTPException, Header

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config_loader import load_workspace  # noqa: E402
from core.db import get_engine  # noqa: E402


# ---------- request-time env checks ----------

def _api_key() -> str:
    """Fail-fast on missing API_KEY at request time. We defer the
    check (rather than failing at import) so test clients can monkey
    the env var before each request."""
    key = os.environ.get("API_KEY")
    if not key:
        raise HTTPException(
            500,
            "server misconfigured: API_KEY env var is not set",
        )
    return key


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. Compares constant-time so the secret can't
    leak via timing. The frontend sends:
        Authorization: Bearer <API_KEY>
    """
    expected = _api_key()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "invalid api key")


def _ws_path() -> str:
    ws = os.environ.get("INVESTOR_WORKSPACE")
    if not ws:
        raise HTTPException(
            500,
            "server misconfigured: INVESTOR_WORKSPACE env var is not set",
        )
    return ws


def _engine_and_ws():
    """Load workspace + engine. Not cached -- engine creation is
    cheap; caching across requests risks stale config when files
    on disk change out-of-band (e.g. operator edits YAML)."""
    ws = load_workspace(_ws_path())
    return get_engine(ws.db_url), ws


def _actor() -> str:
    return os.environ.get("API_OPERATOR", "api-client")


def _allow_example_domains_args() -> list[str]:
    """Expose fixture-domain bypass only when the API operator opts in.

    The CLI flag is useful for local/fixture demos, but the hosted
    API should not silently weaken production guards for browser
    clients.
    """
    raw = os.environ.get("API_ALLOW_EXAMPLE_DOMAINS", "")
    if raw.lower() in {"1", "true", "yes", "on"}:
        return ["--allow-example-domains"]
    return []


def _run_cli(
    *args: str, timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Shell out to scripts/<name>. The CLI scripts use the same
    workspace lock + audit log the operator path uses; the API just
    invokes them and surfaces the output.
    """
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / args[0]), *args[1:]]
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, "USER": _actor()},
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )
