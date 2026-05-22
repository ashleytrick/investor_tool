"""Run logging. Every script execution opens a RunLogger context that writes a
row to `runs`; per-record failures go to `run_errors`. Silent failures are
forbidden, so every run ends with an explicit processed/succeeded/failed/skipped
summary printed and persisted.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from core.db import run_errors, runs
from core.llm.client import LLMUsage


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunLogger:
    def __init__(self, engine: Engine, workspace_name: str, stage: str):
        self.engine = engine
        self.workspace_name = workspace_name
        self.stage = stage
        self.run_id: int | None = None
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        self._llm_usage: LLMUsage | None = None
        self._t0 = 0.0
        self._errors: list[tuple] = []

    def __enter__(self) -> "RunLogger":
        self._t0 = time.monotonic()
        with self.engine.begin() as conn:
            result = conn.execute(
                runs.insert().values(
                    workspace=self.workspace_name,
                    stage=self.stage,
                    started_at=_now(),
                )
            )
            self.run_id = int(result.inserted_primary_key[0])
        return self

    def attach_llm_usage(self, usage: LLMUsage) -> None:
        self._llm_usage = usage

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

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = int(time.monotonic() - self._t0)
        usage = self._llm_usage or LLMUsage()
        error_summary = None
        if exc is not None:
            error_summary = f"{exc_type.__name__}: {exc}"
            self.log_error("__run__", "fatal", error_summary)
        elif self._errors:
            error_summary = f"{len(self._errors)} record error(s)"

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
