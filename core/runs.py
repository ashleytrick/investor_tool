"""Run logging. Every script execution opens a RunLogger context that writes a
row to `runs`; per-record failures go to `run_errors`. Silent failures are
forbidden, so every run ends with an explicit processed/succeeded/failed/skipped
summary printed and persisted.

Refactor Batch C adds semantic accounting methods (attempt / succeed /
skip / fail) that wrap the raw counter mutation pattern every script
used to repeat. The raw counters (run.processed, .succeeded, .failed,
.skipped) remain accessible for migration compatibility.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy.engine import Engine

from core.db import run_errors, runs
from core.llm.client import LLMUsage


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunLogger:
    def __init__(self, engine: Engine, workspace_name: str, stage: str,
                 *, pipeline_batch_id: str | None = None):
        self.engine = engine
        self.workspace_name = workspace_name
        self.stage = stage
        # Issue #19: optional pipeline-spanning batch id. Threaded
        # from stage_run / operator_command_run when the operator
        # passed --pipeline-batch. NULL on stages without the flag.
        self.pipeline_batch_id = pipeline_batch_id
        self.run_id: int | None = None
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        self._llm_usage: LLMUsage | None = None
        self._t0 = 0.0
        self._errors: list[tuple] = []
        self._notes: list[str] = []  # informational; joined into error_summary

    def __enter__(self) -> "RunLogger":
        self._t0 = time.monotonic()
        with self.engine.begin() as conn:
            result = conn.execute(
                runs.insert().values(
                    workspace=self.workspace_name,
                    stage=self.stage,
                    started_at=_now(),
                    pipeline_batch_id=self.pipeline_batch_id,
                )
            )
            self.run_id = int(result.inserted_primary_key[0])
        return self

    def attach_llm_usage(self, usage: LLMUsage) -> None:
        self._llm_usage = usage

    def note(self, msg: str) -> None:
        """Add an informational note. Persisted in the run's error_summary
        alongside any errors so audit fields (e.g. bulk-ready approvals) are
        captured even when the run otherwise succeeds."""
        self._notes.append(msg)

    def log_error(self, record_id: str, error_type: str, message: str) -> None:
        self._errors.append((record_id, error_type, message))
        with self.engine.begin() as conn:
            conn.execute(
                run_errors.insert().values(
                    run_id=self.run_id,
                    record_id=record_id,
                    error_type=error_type,
                    error_message=message,
                    occurred_at=_now(),
                )
            )

    # ----- Refactor Batch C: semantic accounting helpers -----

    @contextmanager
    def attempt(self) -> Iterator["RunLogger"]:
        """Bracket one per-record processing block. Increments .processed
        on entry. Caller decides the outcome via .succeed(), .skip(reason),
        or .fail(record_id, type, msg). If neither is called and no
        exception escaped the block, the attempt is implicitly treated as
        succeeded -- this matches the most common loop shape.

        If an UNCAUGHT exception escapes the block, the attempt is counted
        as a fail and a run_errors row is written. Callers that want to
        attach a record_id should catch the exception themselves and call
        run.fail(record_id, type, msg) -- the implicit-fail path uses
        record_id="?" because there's no per-record context available.

        Usage:
            for partner in partners:
                with run.attempt():
                    do_work(partner)
                    # implicit succeed on clean exit
        OR:
            for partner in partners:
                with run.attempt():
                    try:
                        do_work(partner)
                    except KnownError as e:
                        run.fail(partner.id, type(e).__name__, str(e))
        """
        self.processed += 1
        self._attempt_resolved = False
        try:
            yield self
        except BaseException as exc:
            # Finding 3: previously the finally-only path counted an
            # uncaught exception as a SUCCESS because _attempt_resolved
            # was still False. That silently turned crashes into greens.
            # Mark as fail and re-raise so the loop's outer except (if
            # any) still sees the exception.
            if not self._attempt_resolved:
                self.failed += 1
                self._attempt_resolved = True
                # Best-effort audit. record_id is unknown at this layer.
                try:
                    self.log_error("?", type(exc).__name__, str(exc))
                except Exception:  # noqa: BLE001
                    # Logging itself failed -- don't mask the original.
                    pass
            raise
        finally:
            if not self._attempt_resolved:
                # Clean exit (no exception, no explicit outcome).
                self.succeeded += 1
            self._attempt_resolved = False

    def succeed(self) -> None:
        """Mark the current .attempt() as succeeded. Idempotent if called
        twice in the same block; only the first call counts."""
        if not getattr(self, "_attempt_resolved", False):
            self.succeeded += 1
            self._attempt_resolved = True

    def skip(self, reason: str | None = None) -> None:
        """Mark the current .attempt() as skipped. Optional reason lands
        as a run.note so the audit captures WHY rows were skipped."""
        if not getattr(self, "_attempt_resolved", False):
            self.skipped += 1
            self._attempt_resolved = True
            if reason:
                self.note(f"skip: {reason}")

    def fail(self, record_id: str, error_type: str, message: str) -> None:
        """Mark the current .attempt() as failed AND write a run_errors
        row. One call replaces the old `run.failed += 1; run.log_error(...)`
        pattern that was easy to half-do."""
        if not getattr(self, "_attempt_resolved", False):
            self.failed += 1
            self._attempt_resolved = True
        self.log_error(record_id, error_type, message)

    def is_clean(self) -> bool:
        """True when the run processed at least one record and none failed."""
        return self.processed > 0 and self.failed == 0

    def all_skipped(self) -> bool:
        """True when the run processed records but every one was skipped
        (succeeded == 0 AND failed == 0). Used by Stage 6's #29 check
        for 'every partner had no qualifying signals'."""
        return (
            self.processed > 0
            and self.succeeded == 0
            and self.failed == 0
        )

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = int(time.monotonic() - self._t0)
        usage = self._llm_usage or LLMUsage()
        parts: list[str] = []
        # SystemExit raised mid-stage is an intentional abort
        # (e.g. stage_runner's preflight refusal). Don't log it as
        # "fatal" -- the refusal note via run.note() already covers it
        # and a phantom "SystemExit: 3" line in error_summary would
        # confuse audits.
        is_systemexit = isinstance(exc, SystemExit)
        if exc is not None and not is_systemexit:
            err = f"{exc_type.__name__}: {exc}"
            parts.append(err)
            self.log_error("__run__", "fatal", err)
        elif self._errors:
            parts.append(f"{len(self._errors)} record error(s)")
        parts.extend(self._notes)
        error_summary = "; ".join(parts) if parts else None

        with self.engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.run_id == self.run_id)
                .values(
                    completed_at=_now(),
                    records_processed=self.processed,
                    records_succeeded=self.succeeded,
                    records_failed=self.failed,
                    records_skipped=self.skipped,
                    llm_calls_made=usage.calls_made,
                    llm_input_tokens=usage.input_tokens,
                    llm_output_tokens=usage.output_tokens,
                    elapsed_seconds=elapsed,
                    error_summary=error_summary,
                )
            )
        print(
            f"[run {self.run_id}] stage={self.stage} "
            f"processed={self.processed} succeeded={self.succeeded} "
            f"failed={self.failed} skipped={self.skipped} "
            f"elapsed={elapsed}s"
        )
        # Do not suppress exceptions.
        return False
