"""Workspace-aware config loader. Built FIRST; every script imports from it.

Code in core/ and scripts/ must stay tenant-agnostic. All per-instance state
is read through a Workspace instance constructed from a --workspace path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

# Repo root = parent of the directory containing this file.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Workspace:
    """Resolves all per-workspace paths, config files, and environment values.

    Environment resolution order (later wins):
      1. repo-root .env
      2. clients/{workspace}/.env
      3. process environment variables
    """

    def __init__(self, workspace_arg: str):
        # Accept either "clients/test_workspace" or an absolute path.
        ws = Path(workspace_arg)
        if not ws.is_absolute():
            ws = (REPO_ROOT / ws).resolve()
        if not ws.exists():
            raise FileNotFoundError(f"Workspace directory not found: {ws}")
        self.path: Path = ws
        self.name: str = ws.name
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
        return f"sqlite:///{self.db_path}"

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
