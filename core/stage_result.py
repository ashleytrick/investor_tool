"""Standardized stage exit codes.

Every pipeline script returns one of these. The runner (core.stage_runner)
maps internal state (run.failed, ctx.refuse()) to a StageResult. cron
wrappers can distinguish "the operator's safety gate fired" from "the
LLM crashed" by testing the specific code.

  OK                   = 0  clean completion, no row failures
  USAGE_ERROR          = 1  CLI / config / argparse error -- operator
                            mistake, no work attempted
  OPERATIONAL_FAILURE  = 2  one or more rows failed during processing,
                            OR transport / DB / LLM error caught
  REFUSED_UNSAFE       = 3  a safety gate consciously refused work:
                            mode=fixture, freshness fail, batch QA hard
                            failure, required-source fail, etc. The
                            operator overrides via an explicit flag.

Cron wrappers that previously treated `rc == 2` as "any failure" can
stay correct with `[ $rc -ge 2 ]`. Wrappers that want to distinguish
"data drift" (2) from "I refuse to ship this" (3) now can.
"""
from __future__ import annotations

from enum import IntEnum


class StageResult(IntEnum):
    OK = 0
    USAGE_ERROR = 1
    OPERATIONAL_FAILURE = 2
    REFUSED_UNSAFE = 3
