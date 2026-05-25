"""Unit tests for core/csv_ingest.py (Refactor item 4)."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import REPO_ROOT  # noqa: F401 - sys.path side-effect

from core.csv_ingest import (
    CsvIngestSchema,
    RowError,
    ingest_csv,
    in_set,
    looks_like_email,
    looks_like_url,
    require_field,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_required_headers_short_circuits(tmp_path: Path) -> None:
    """If a required header is absent the parser doesn't try to interpret
    rows -- caller treats missing_headers as a hard fail upstream."""
    p = _write(tmp_path / "x.csv", "partner_id,source_url\nfoo,https://x\n")
    schema = CsvIngestSchema(
        required_headers={"partner_id", "source_type", "source_url"},
    )
    result = ingest_csv(p, schema)
    assert result.missing_headers == ["source_type"]
    assert result.rows == []
    assert result.row_errors == []


def test_missing_file_reports_required_as_missing(tmp_path: Path) -> None:
    """Non-existent CSV behaves the same as a CSV that exists but
    declares no headers: missing_headers contains every required name,
    no rows or row errors."""
    schema = CsvIngestSchema(required_headers={"a", "b"})
    result = ingest_csv(tmp_path / "nope.csv", schema)
    assert result.missing_headers == ["a", "b"]
    assert result.rows == []


def test_strict_unknown_headers_reports_extras(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "x.csv",
        "partner_id,source_url,bogus\n"
        "foo,https://x,whatever\n",
    )
    schema = CsvIngestSchema(
        required_headers={"partner_id", "source_url"},
        strict_unknown_headers=True,
    )
    result = ingest_csv(p, schema)
    assert result.unknown_headers == ["bogus"]
    # Strict-mode still parses rows so callers can inspect both layers.
    assert len(result.rows) == 1


def test_per_row_validators_collect_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "outcomes.csv",
        "partner_id,email\n"
        "real,priya@example.com\n"
        ",blank@example.com\n"        # row 2: missing partner_id
        "stranger,bad-email\n"        # row 3: unknown partner + bad email shape
    )
    known = {"real", "marcus"}
    schema = CsvIngestSchema(
        required_headers={"partner_id", "email"},
        row_validators=(
            require_field("partner_id"),
            in_set("partner_id", known, error_type="unknown_partner"),
            looks_like_email("email"),
        ),
    )
    result = ingest_csv(p, schema)
    assert [r["partner_id"] for r in result.rows] == ["real"]
    # Each row error stops at the first failing validator (short-circuit
    # so the operator sees the most specific problem first).
    errs = result.row_errors
    assert len(errs) == 2
    assert errs[0].row_num == 2 and errs[0].error_type == "missing_field"
    assert errs[1].row_num == 3 and errs[1].error_type == "unknown_partner"


def test_blank_lines_are_silently_skipped(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "x.csv",
        "partner_id,email\n"
        "real,priya@example.com\n"
        ",,\n"  # all-empty row; DictReader would otherwise emit one
        "marcus,marcus@example.com\n",
    )
    schema = CsvIngestSchema(
        required_headers={"partner_id", "email"},
        row_validators=(require_field("partner_id"),),
    )
    result = ingest_csv(p, schema)
    assert [r["partner_id"] for r in result.rows] == ["real", "marcus"]
    assert result.row_errors == []


def test_looks_like_url_rejects_garbage(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "x.csv",
        "source_url\nhttps://ok.example/path\nnot a url\n",
    )
    schema = CsvIngestSchema(
        required_headers={"source_url"},
        row_validators=(looks_like_url("source_url"),),
    )
    result = ingest_csv(p, schema)
    assert [r["source_url"] for r in result.rows] == ["https://ok.example/path"]
    assert result.row_errors[0].error_type == "invalid_url"


def test_row_error_record_id_falls_back(tmp_path: Path) -> None:
    """When no partner_id/fund_id/source_url is present, record_id
    falls back to row_<n> so run_errors still gets a useful label."""
    p = _write(
        tmp_path / "x.csv",
        "category,value\nfoo,bar\n,bar\n",
    )

    def reject_empty_category(row: dict[str, str]) -> None:
        if not row.get("category"):
            raise ValueError("category must be set")

    schema = CsvIngestSchema(
        required_headers={"category", "value"},
        row_validators=(reject_empty_category,),
    )
    result = ingest_csv(p, schema)
    assert result.row_errors[0].record_id == "row_2"


def test_row_error_namedtuple_field_count() -> None:
    """Guard against accidentally changing RowError shape."""
    err = RowError(row_num=1, error_type="x", message="msg", record_id="r")
    assert err._fields == ("row_num", "error_type", "message", "record_id")
