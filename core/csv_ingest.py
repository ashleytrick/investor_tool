"""Operator-CSV ingestion validator (Refactor item 4).

Stage 4, record_outcome, set_partner_email and a handful of future
importers all read operator-edited CSVs with the same shape concerns:

  - required headers must be present (otherwise every row silently
    treats the missing column as empty);
  - per-row strip / empty-field checks;
  - per-row foreign-key checks (partner_id in known partners, etc.);
  - per-row format checks (looks-like-email, looks-like-url);
  - the OPERATOR cares about line numbers so they can fix the file.

Today each caller re-implements this. The bespoke versions drift: one
treats unknown partner_id as a hard fail, another silently skips,
another logs but doesn't bump the failed counter. This module collapses
that into one shape with explicit row outcomes so the callers stay thin.

Usage::

    schema = CsvIngestSchema(
        required_headers={"partner_id", "source_type", "source_url"},
        row_validators=[
            require_field("partner_id"),
            require_field("source_url"),
            in_set("partner_id", known_partner_ids,
                   error_type="unknown_partner"),
        ],
    )
    result = ingest_csv(csv_path, schema)
    if result.missing_headers:
        run.fail(...); raise ValueError(...)
    for err in result.row_errors:
        run.log_error(str(err.row_num), err.error_type, err.message)
    for row in result.rows:
        ...  # well-formed rows

The validators below are the common shapes. Tests live in
tests/test_config_and_validators.py (Refactor item 23 split).
"""
from __future__ import annotations

import csv as _csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NamedTuple


# A row validator is a callable that takes the parsed row dict and
# raises ValueError if the row should be rejected. The error message
# becomes the run_errors message.
RowValidator = Callable[[dict[str, str]], None]


@dataclass
class CsvIngestSchema:
    """Declarative schema for an operator-CSV importer."""

    required_headers: frozenset[str]
    optional_headers: frozenset[str] = field(default_factory=frozenset)
    # Each validator runs after the row is parsed. Validators raise
    # ValueError with a human-readable message; the message ends up in
    # run_errors.
    row_validators: tuple[RowValidator, ...] = ()
    # When True, headers outside required ∪ optional cause an error.
    # Default False: extra columns are silently ignored so adding new
    # columns to operator-edited CSVs doesn't break older importers.
    strict_unknown_headers: bool = False

    def __post_init__(self) -> None:
        # Normalize to frozensets so callers can pass plain sets/lists.
        object.__setattr__(self, "required_headers",
                           frozenset(self.required_headers))
        object.__setattr__(self, "optional_headers",
                           frozenset(self.optional_headers))


class RowError(NamedTuple):
    row_num: int  # 1-indexed; row 1 is the first data row, not header
    error_type: str  # short string for run_errors.error_type
    message: str  # human-readable detail
    record_id: str  # best-effort identifier for run_errors (often partner_id)


@dataclass
class CsvIngestResult:
    rows: list[dict[str, str]]
    headers: list[str]  # whatever the file declared
    missing_headers: list[str]  # required - declared
    unknown_headers: list[str]  # declared - (required ∪ optional); empty when strict_unknown_headers=False
    row_errors: list[RowError]

    @property
    def header_ok(self) -> bool:
        return not self.missing_headers and not self.unknown_headers


def ingest_csv(path: Path, schema: CsvIngestSchema) -> CsvIngestResult:
    """Parse + validate `path`. Never raises on row-level problems;
    everything lands in CsvIngestResult. Callers decide how to handle
    headers vs. row errors (typically: missing_headers is a hard fail,
    row_errors lands in run_errors and the operator fixes the file).
    """
    if not path.exists():
        return CsvIngestResult(
            rows=[], headers=[], missing_headers=sorted(schema.required_headers),
            unknown_headers=[], row_errors=[],
        )
    rows: list[dict[str, str]] = []
    row_errors: list[RowError] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = _csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        declared = set(headers)
        missing = sorted(schema.required_headers - declared)
        unknown: list[str] = []
        if schema.strict_unknown_headers:
            allowed = schema.required_headers | schema.optional_headers
            unknown = sorted(declared - allowed)
        if missing or unknown:
            # Header-level break: don't try to interpret rows whose
            # required columns aren't there. (Unknown-header strict
            # mode falls through to row parsing so the caller can still
            # see the data if they want.)
            if missing:
                return CsvIngestResult(
                    rows=[], headers=headers, missing_headers=missing,
                    unknown_headers=unknown, row_errors=[],
                )
        # Strip per-row values so downstream checks don't trip over
        # whitespace-only fields. csv.DictReader can yield None for the
        # value when a row is short -- coerce to "".
        for i, raw in enumerate(reader, start=1):
            row = {
                k: (str(raw.get(k) or "").strip())
                for k in (headers or list(raw.keys()))
            }
            # Skip totally-blank lines silently.
            if not any(row.values()):
                continue
            best_record_id = (
                row.get("partner_id")
                or row.get("fund_id")
                or row.get("source_url")
                or f"row_{i}"
            )
            row_failed = False
            for validator in schema.row_validators:
                try:
                    validator(row)
                except ValueError as exc:
                    row_errors.append(RowError(
                        row_num=i,
                        error_type=getattr(exc, "error_type", "csv_validation"),
                        message=str(exc),
                        record_id=best_record_id,
                    ))
                    row_failed = True
                    break
            if not row_failed:
                rows.append(row)
    return CsvIngestResult(
        rows=rows, headers=headers, missing_headers=[],
        unknown_headers=unknown, row_errors=row_errors,
    )


# ----- common row validators -----


class _TypedValueError(ValueError):
    """ValueError carrying an error_type tag for run_errors."""

    def __init__(self, message: str, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def require_field(field_name: str) -> RowValidator:
    """Row must have a non-empty `field_name`."""

    def _check(row: dict[str, str]) -> None:
        if not row.get(field_name):
            raise _TypedValueError(
                f"row missing required field {field_name!r}",
                error_type="missing_field",
            )

    return _check


def in_set(field_name: str, allowed: Iterable[str],
           *, error_type: str = "unknown_value") -> RowValidator:
    """Row's `field_name` value must be in `allowed`. Skips empty values
    (let require_field() catch those separately so the operator sees the
    real reason)."""
    allowed_set = set(allowed)

    def _check(row: dict[str, str]) -> None:
        v = row.get(field_name) or ""
        if not v:
            return
        if v not in allowed_set:
            raise _TypedValueError(
                f"{field_name}={v!r} not in known set "
                f"({len(allowed_set)} allowed)",
                error_type=error_type,
            )

    return _check


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(field_name: str = "email") -> RowValidator:
    """Row's `field_name` must look like an email address. Skips empty
    values so callers can opt to allow blank emails via a separate
    require_field() if needed."""

    def _check(row: dict[str, str]) -> None:
        v = row.get(field_name) or ""
        if not v:
            return
        if not _EMAIL_RE.match(v):
            raise _TypedValueError(
                f"{field_name}={v!r} does not look like an email address",
                error_type="invalid_email",
            )

    return _check


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")


def looks_like_url(field_name: str = "source_url") -> RowValidator:
    """Row's `field_name` must look like an http(s) URL. Skips empty
    values."""

    def _check(row: dict[str, str]) -> None:
        v = row.get(field_name) or ""
        if not v:
            return
        if not _URL_RE.match(v):
            raise _TypedValueError(
                f"{field_name}={v!r} does not look like an http(s) URL",
                error_type="invalid_url",
            )

    return _check
