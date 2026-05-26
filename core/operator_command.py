"""Operator-command runner (Finding 4 from the Slices 1-11 review).

`core/stage_runner.stage_run` owns workspace-lock + pre-stage SQLite
backup + RunLogger for pipeline stages (01-08). Operator CLIs that
mutate state (approve_draft, reject_draft, import_partner_emails_apollo,
set_*) bypassed `stage_run` entirely, so:

  - Two operator runs could race against an in-flight Stage 7 / 8
    (no workspace lock).
  - The mutation went through without a fresh backup, so a buggy
    operator action couldn't be rolled back.
  - The action wasn't visible in the `runs` table audit trail.

This module provides the operator-side counterpart with the same
protections but without the pipeline-stage extras (no LLM client, no
preflight config validation, no example-domain policy -- the operator
is responsible for that). Mutating CLIs replace their bare
RunLogger / get_engine boilerplate with:

    with operator_command_run(args, stage="approve_draft") as ctx:
        # ctx.ws, ctx.engine, ctx.run
        ...
    return ctx.exit_code
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from sqlalchemy.engine import Engine

from core.backups import backup_before_stage, pre_migration_backup
from core.banner import print_banner
from core.config_loader import Workspace, load_workspace
from core.db import get_engine
from core.runlock import RunLockBusy, workspace_lock
from core.runs import RunLogger
from core.stage_result import StageResult


@dataclass
class OperatorContext:
    args: Any
    ws: Workspace
    engine: Engine
    run: RunLogger
    stage: str
    _explicit_exit: StageResult | None = None

    def refuse(self, reason: str,
               *, code: StageResult = StageResult.OPERATIONAL_FAILURE) -> None:
        self.run.note(reason)
        self.run.failed = max(self.run.failed, 1)
        self._explicit_exit = code

    def refuse_unsafe(self, reason: str) -> None:
        self.refuse(reason, code=StageResult.REFUSED_UNSAFE)

    def usage_error(self, reason: str) -> None:
        self.refuse(reason, code=StageResult.USAGE_ERROR)

    @property
    def exit_code(self) -> int:
        if self._explicit_exit is not None:
            return int(self._explicit_exit)
        if self.run.failed > 0:
            return int(StageResult.OPERATIONAL_FAILURE)
        return int(StageResult.OK)


@contextmanager
def operator_command_run(
    args: Any,
    *,
    stage: str,
    skip_backup: bool = False,
    skip_banner: bool = False,
) -> Iterator[OperatorContext]:
    """Open an operator-command execution scope.

    Args:
      args: argparse Namespace with `.workspace` (or accept the
        INVESTOR_WORKSPACE env fallback).
      stage: the command label recorded in runs.stage + used as the
        backup tag. Match the script's STAGE constant.
      skip_backup: opt out of the pre-command DB backup. Reserved for
        truly read-mostly commands that just want the lock + audit row
        (none today).
      skip_banner: skip the workspace banner print -- read-only list
        commands sometimes want to keep their stdout machine-readable.

    Lock contention exits 2 (OPERATIONAL_FAILURE) before any DB write.
    Backup failure prints a warning + continues (insurance, not
    blocking). RunLogger commits the run row in `runs` on exit.
    """
    ws = load_workspace(getattr(args, "workspace", None))
    if not skip_banner:
        print_banner(ws, stage=stage)
    # Acquire the workspace lock BEFORE get_engine() so two operator
    # commands can't race against the same ALTER TABLE / migration
    # backfill on a real workspace DB.
    try:
        _lock_cm = workspace_lock(ws.path, stage=stage)
        _lock_cm.__enter__()
    except RunLockBusy as exc:
        print(f"[{stage}] REFUSED: {exc}")
        raise SystemExit(int(StageResult.OPERATIONAL_FAILURE))

    # Snapshot the DB before get_engine() runs migrations. No-op on
    # a fresh workspace; gives operator-upgraded workspaces a tagged
    # pre_migration backup. Must be inside the lock.
    pre_migration_backup(ws.path, db_path=ws.db_path)
    engine = get_engine(ws.db_url)

    if not skip_backup:
        backup_before_stage(ws.path, stage=stage, db_path=ws.db_path)

    try:
        with RunLogger(engine, ws.name, stage) as run:
            ctx = OperatorContext(
                args=args, ws=ws, engine=engine, run=run, stage=stage,
            )
            yield ctx
    finally:
        try:
            _lock_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- don't mask the real error
            pass
