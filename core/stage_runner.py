"""Shared per-stage runner: workspace load, preflight, banner, engine,
LLM client, RunLogger, llm.usage attach, exit-code policy.

Every pipeline script today repeats ~15 lines of boilerplate around the
actual stage work. The repetition was the source of every "Stage X exits
0 on row-level failure" review finding -- the fix had to be applied N
times. This module collapses that into one context manager so the next
safety check is a one-line edit, not N.

Usage:

    from core.stage_runner import stage_run

    def main() -> int:
        parser = argparse.ArgumentParser(...)
        add_workspace_arg(parser)
        # stage-specific args
        args = parser.parse_args()

        with stage_run(
            args, stage="06_score_candidates",
            require_anthropic=False,
        ) as ctx:
            # ctx.ws, ctx.engine, ctx.run, ctx.llm, ctx.args
            for partner in load_partners(ctx.engine):
                ctx.run.processed += 1
                try:
                    do_work(partner)
                    ctx.run.succeeded += 1
                except KnownError as e:
                    ctx.run.failed += 1
                    ctx.run.log_error(partner.id, type(e).__name__, str(e))
        return ctx.exit_code

The context manager handles:
  * load_workspace() OR exits with StageResult.USAGE_ERROR if missing
  * preflight_or_exit() (still calls sys.exit on config issues; see
    Refactor Batch B for the StageResult.REFUSED_UNSAFE replacement)
  * print_banner() with the correct stage label
  * get_engine() so callers don't repeat the data_dir mkdir dance
  * LLMClient(workspace=ws) when require_llm=True
  * RunLogger(engine, ws.name, stage) auto-entered + auto-exited so
    the run row always lands in `runs`
  * run.attach_llm_usage(llm.usage) so cost accounting is automatic
  * exit_code property maps run.failed -> 2 (operational failure), 0
    otherwise. Stages that need to return non-zero for other reasons
    (refused unsafe action, calibration gate, etc.) set
    ctx.refuse(reason) which marks the run failed AND records the
    refusal note. Future Batch B will distinguish 2 vs 3.

Scripts can still raise inside the `with` block; the RunLogger __exit__
records the fatal error in run_errors and re-raises, so the failure is
visible AND the process exits non-zero via the normal Python flow.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from sqlalchemy.engine import Engine

from core.backups import backup_before_stage
from core.banner import print_banner
from core.config_loader import Workspace, load_workspace
from core.db import get_engine
from core.runlock import RunLockBusy, workspace_lock
from core.runs import RunLogger
from core.stage_result import StageResult
from core.validate_config import preflight_or_exit, validate_workspace_config


@dataclass
class StageContext:
    """Everything a stage's body needs. Created by stage_run()."""
    args: Any
    ws: Workspace
    engine: Engine
    run: RunLogger
    llm: Optional[Any]  # core.llm.client.LLMClient; Any to avoid circular
    stage: str
    _explicit_exit: Optional[StageResult] = None

    def refuse(self, reason: str,
               *, code: StageResult = StageResult.OPERATIONAL_FAILURE) -> None:
        """Mark the run as refused (a gate fired) with an explicit reason
        that lands in runs.error_summary. stage_run picks up
        `_explicit_exit` and exposes it via .exit_code.

        Default code is OPERATIONAL_FAILURE (=2) for back-compat with
        the pre-refactor exit-code convention. Call refuse_unsafe()
        when the gate is specifically a safety refusal (distinguishes
        operator-action-needed from LLM/data-failure for cron).
        """
        self.run.note(reason)
        self.run.failed = max(self.run.failed, 1)
        self._explicit_exit = code

    def refuse_unsafe(self, reason: str) -> None:
        """Mark the run as refused for a SAFETY reason (mode=fixture,
        freshness fail, batch QA hard fail, required-source fail).
        Maps to StageResult.REFUSED_UNSAFE (=3) so cron wrappers can
        distinguish 'I refuse to ship this' from 'the LLM crashed'.
        """
        self.refuse(reason, code=StageResult.REFUSED_UNSAFE)

    def usage_error(self, reason: str) -> None:
        """Mark the run as a CLI / config usage error (exit 1). Same
        semantics as refuse() but uses StageResult.USAGE_ERROR so cron
        wrappers can distinguish operator mistakes from data drift."""
        self.refuse(reason, code=StageResult.USAGE_ERROR)

    @property
    def exit_code(self) -> int:
        """StageResult mapped to an int per the policy in stage_result.py.

        Priority:
          1. explicit ctx.refuse() / refuse_unsafe() / usage_error()
             -> their code
          2. run.failed > 0  -> OPERATIONAL_FAILURE
          3. otherwise       -> OK
        """
        if self._explicit_exit is not None:
            return int(self._explicit_exit)
        if self.run.failed > 0:
            return int(StageResult.OPERATIONAL_FAILURE)
        return int(StageResult.OK)


@contextmanager
def stage_run(
    args: Any,
    *,
    stage: str,
    require_anthropic: bool = False,
    require_attio: bool = False,
    require_examples: bool = False,
    require_llm: bool = True,
    skip_preflight: bool = False,
) -> Iterator[StageContext]:
    """Open a stage execution scope.

    Args:
      args: argparse Namespace -- must have args.workspace (or accept
        the INVESTOR_WORKSPACE env fallback via load_workspace).
      stage: the stage label recorded in runs.stage and shown in the
        banner. Matches the script's STAGE constant.
      require_anthropic / require_attio / require_examples: forwarded to
        preflight_or_exit(). Stages that already conditionally enable
        these (e.g. Stage 2's `require_anthropic=not args.fixtures`)
        pass the computed bool.
      require_llm: when True (default) instantiates LLMClient and
        attaches its usage tracker to RunLogger. Stages that never call
        the LLM (status.py, manual_override.py, etc.) pass False to
        avoid the import cost.
      skip_preflight: status.py + diagnostic CLIs disable preflight so
        operators can run them on broken workspaces. Stage scripts
        should always leave this False.
    """
    ws = load_workspace(getattr(args, "workspace", None))
    print_banner(ws, stage=stage)
    engine = get_engine(ws.db_url)
    # Slice 4: workspace run-lock. Refuse to start a stage when
    # another stage is already running against the same workspace --
    # prevents SQLite races + corrupted exports. Read-only / status
    # callers skip via skip_preflight (those use `Workspace` without
    # opening RunLogger / writing).
    if not skip_preflight:
        try:
            _lock_cm = workspace_lock(ws.path, stage=stage)
            _lock_cm.__enter__()
        except RunLockBusy as exc:
            # Refuse loudly + exit non-zero. Don't open RunLogger --
            # we never started running.
            print(f"[{stage}] REFUSED: {exc}")
            raise SystemExit(int(StageResult.OPERATIONAL_FAILURE))
    else:
        _lock_cm = None
    # Slice 5: pre-stage SQLite backup for destructive stages. The
    # whitelist lives in core.backups.stages_needing_backup; a
    # backup failure prints a WARN and continues (backups are
    # insurance, never a blocker).
    if not skip_preflight:
        backup_before_stage(
            ws.path, stage=stage, db_path=ws.db_path,
        )
    # Launch-blocker fix: preflight runs INSIDE the RunLogger context
    # so a missing API key / config issue lands as a refusal row in
    # `runs` (status.py / audit can see it) rather than being a
    # silent sys.exit(2) before any row was created. The previous
    # shape ran preflight_or_exit() before RunLogger opened.
    llm = None
    if require_llm:
        # Import lazily so non-LLM stages don't pay the cost.
        from core.llm.client import LLMClient
        llm = LLMClient(workspace=ws)
    try:
        with RunLogger(engine, ws.name, stage) as run:
            if llm is not None:
                run.attach_llm_usage(llm.usage)
            ctx = StageContext(
                args=args, ws=ws, engine=engine, run=run, llm=llm,
                stage=stage,
            )
            if not skip_preflight:
                issues = validate_workspace_config(
                    ws,
                    require_anthropic=require_anthropic,
                    require_attio=require_attio,
                    require_examples=require_examples,
                )
                if issues:
                    summary = (
                        f"REFUSED: workspace config has {len(issues)} "
                        f"issue(s): " + "; ".join(issues)
                    )
                    print(f"[{stage}] REFUSED: workspace config has "
                          f"{len(issues)} issue(s):")
                    for s in issues:
                        print(f"  - {s}")
                    print(f"[{stage}] edit "
                          f"clients/{ws.name}/config/*.yaml "
                          f"(and prompts/examples/) then re-run.")
                    ctx.refuse_unsafe(summary)
                    yield ctx
                    return
            yield ctx
    finally:
        # Release the workspace lock no matter how the stage exits.
        if _lock_cm is not None:
            try:
                _lock_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - don't mask the real error
                pass
