"""Workspace-aware config loader. Built FIRST; every script imports from it.

Code in core/ and scripts/ must stay tenant-agnostic. All per-instance state
is read through a Workspace instance constructed from a --workspace path.
"""
from __future__ import annotations

import hashlib
import os
import urllib.parse
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

# Repo root = parent of the directory containing this file.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    # Batch 14 (#304): a malformed YAML file used to surface as a raw
    # yaml.YAMLError stack trace from somewhere deep in the LLM client
    # pipeline. Catch + reframe with the filename + a hint pointing at
    # the offending line so the operator can fix it.
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:  # noqa: BLE001
        mark = getattr(exc, "problem_mark", None)
        loc = (
            f" at line {mark.line + 1} col {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise SystemExit(
            f"config error: {path} is not valid YAML{loc}: {exc}"
        ) from exc


class Workspace:
    """Resolves all per-workspace paths, config files, and environment values.

    Environment resolution order (later wins):
      1. repo-root .env
      2. clients/{workspace}/.env
      3. process environment variables
    """

    def __init__(self, workspace_arg: str):
        # Accept either "clients/test_workspace" or an absolute path.
        ws_raw = Path(workspace_arg)
        if not ws_raw.is_absolute():
            ws = (REPO_ROOT / ws_raw).resolve()
        else:
            ws = ws_raw.resolve()
        if not ws.exists():
            # Batch 14 (#300): if the operator typo'd INVESTOR_WORKSPACE we
            # used to silently raise FileNotFoundError. Hint at the most
            # likely cause + show available siblings under clients/.
            siblings = sorted(
                p.name for p in (REPO_ROOT / "clients").glob("*")
                if p.is_dir()
            ) if (REPO_ROOT / "clients").exists() else []
            sibling_hint = (
                f" Available under clients/: {siblings}"
                if siblings else ""
            )
            raise FileNotFoundError(
                f"Workspace directory not found: {ws} "
                f"(from {workspace_arg!r}).{sibling_hint}"
            )
        self.path: Path = ws
        # Batch 14 (#302): basename collisions across absolute workspaces
        # ("/foo/clients/acme" vs "/bar/clients/acme") used to map to the
        # same `name`, so run logs + cross-workspace stats keyed by name
        # silently shared between two unrelated workspaces. Append an 8-char
        # path hash when the workspace lives OUTSIDE the repo's clients/
        # dir, where collisions are most likely. In-repo paths
        # (clients/<name>/) keep the bare name for backward compatibility.
        bare_name = ws.name
        in_repo = (REPO_ROOT / "clients" / bare_name).resolve() == ws
        if in_repo:
            self.name: str = bare_name
        else:
            hash8 = hashlib.sha1(
                str(ws).encode("utf-8")
            ).hexdigest()[:8]
            self.name = f"{bare_name}-{hash8}"
        self.repo_root: Path = REPO_ROOT

        # Standard subdirectories.
        self.config_dir = ws / "config"
        self.data_dir = ws / "data"
        self.raw_dir = self.data_dir / "raw"
        self.fixtures_dir = self.data_dir / "fixtures"
        self.exports_dir = ws / "exports"
        self.examples_dir = ws / "prompts" / "examples"
        self.db_path = self.data_dir / "pipeline.db"
        # Do NOT mkdir here. Loading a workspace should be side-effect free so
        # status.py / validate / typo'd paths don't dirty the filesystem.
        # init_workspace.py creates the tree; write-stages (get_engine for
        # data_dir, write_review_queue for exports_dir) ensure their own
        # target dirs exist just before writing.

        # Config files.
        self.company = _load_yaml(self.config_dir / "company.yaml")
        self.axes = _load_yaml(self.config_dir / "axes.yaml")
        self.sources = _load_yaml(self.config_dir / "sources.yaml")
        self.attio = _load_yaml(self.config_dir / "attio.yaml")

        # Environment values, layered.
        self._env: dict[str, str] = {}
        self._env.update({k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None})
        self._env.update({k: v for k, v in dotenv_values(ws / ".env").items() if v is not None})

    def env(self, key: str, default: str | None = None) -> str | None:
        """Process env overrides both .env files; otherwise layered .env wins."""
        if key in os.environ and os.environ[key]:
            return os.environ[key]
        val = self._env.get(key)
        return val if val else default

    @property
    def db_url(self) -> str:
        # Batch 14 (#303): URL-quote the path so workspaces under
        # directories containing spaces, # or ? don't break SQLAlchemy's
        # URL parser. Forward slashes stay unencoded (path separator) and
        # the leading triple-slash convention is preserved.
        quoted = urllib.parse.quote(str(self.db_path), safe="/")
        return f"sqlite:///{quoted}"

    def __repr__(self) -> str:
        return f"<Workspace name={self.name!r} path={self.path}>"


def add_workspace_arg(parser) -> None:
    """Shared argparse wiring so every script accepts --workspace identically.

    Not strictly required: scripts can also resolve the workspace from the
    INVESTOR_WORKSPACE env var. The explicit --workspace arg wins when both
    are present.
    """
    parser.add_argument(
        "--workspace",
        default=None,
        help="Path to the workspace dir, e.g. clients/test_workspace. "
             "Falls back to the INVESTOR_WORKSPACE env var if omitted.",
    )


def load_workspace(workspace_arg: str | None) -> Workspace:
    """Resolve --workspace OR INVESTOR_WORKSPACE OR raise a clear error."""
    ws = workspace_arg or os.environ.get("INVESTOR_WORKSPACE")
    if not ws:
        raise SystemExit(
            "no workspace specified: pass --workspace clients/{name} or "
            "set INVESTOR_WORKSPACE in your environment."
        )
    return Workspace(ws)
