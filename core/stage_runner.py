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

from core.banner import print_banner
from core.config_loader import Workspace, load_workspace
from core.db import get_engine
from core.runs import RunLogger
from core.validate_config import preflight_or_exit


@dataclass
class StageContext:
    """Everything a stage's body needs. Created by stage_run()."""
    args: Any
    ws: Workspace
    engine: Engine
    run: RunLogger
    llm: Optional[Any]  # core.llm.client.LLMClient; Any to avoid circular
    stage: str
    _explicit_exit: Optional[int] = None

    def refuse(self, reason: str, *, exit_code: int = 2) -> None:
        """Mark the run as refused (safety gate fired) with an explicit
        reason that lands in runs.error_summary. The stage_run context
        manager picks up `_explicit_exit` and returns it.

        Use when the stage is consciously refusing to do work for a
        safety reason (mode=fixture, freshness fail, batch QA fail).
        Distinct from raising, which the runner treats as a fatal error.
        """
        self.run.note(reason)
        self.run.failed = max(self.run.failed, 1)
        self._explicit_exit = exit_code

    @property
    def exit_code(self) -> int:
        """0 when the run is clean, 2 when any per-record failure was
        recorded OR refuse() was called. Refer to Refactor Batch B for
        the planned 1/2/3 split."""
        if self._explicit_exit is not None:
            return self._explicit_exit
        if self.run.failed > 0:
            return 2
        return 0


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
    if not skip_preflight:
        preflight_or_exit(
            ws, stage=stage,
            require_anthropic=require_anthropic,
            require_attio=require_attio,
            require_examples=require_examples,
        )
    print_banner(ws, stage=stage)
    engine = get_engine(ws.db_url)
    llm = None
    if require_llm:
        # Import lazily so non-LLM stages don't pay the cost.
        from core.llm.client import LLMClient
        llm = LLMClient(workspace=ws)
    with RunLogger(engine, ws.name, stage) as run:
        if llm is not None:
            run.attach_llm_usage(llm.usage)
        ctx = StageContext(
            args=args, ws=ws, engine=engine, run=run, llm=llm,
            stage=stage,
        )
        yield ctx
