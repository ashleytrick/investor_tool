"""Writes the CSV review queue at clients/{workspace}/exports/review_queue.csv.

The CSV is the primary deliverable and is overwritten on each Stage 7 run; the
SQLite db retains historical batches. Column order is fixed by PROJECT_BRIEF.
"""
from __future__ import annotations

import csv
from pathlib import Path

# Fixed column order. Stage 7 must supply every key for every row.
CSV_COLUMNS: list[str] = [
    "partner_id",
    "partner_name",
    "partner_title",
    "fund_name",
    "fund_domain",
    "linkedin_url",
    "send_now_priority",
    "composite_fit_score",
    "round_fit_score",
    "round_fit_reasoning",
    "lead_likelihood_score",
    "lead_likelihood_signals",
    "cold_reachability_score",
    "spiky_belief_score",
    "top_signals",
    "recommended_to_send",
    "recommendation_reasoning",
    "email_strategy_used",
    "email_subject_line",
    "outreach_email_draft",
    "conversion_hypothesis",
    "likely_objection",
    "objection_preempted",
    "email_alternate_strategy",
    "email_draft_alternate",
    "followup_email_draft",
    "deck_request_response",
    "template_smell",
    "warm_path_available",
    "outreach_status",
]


def write_review_queue(exports_dir: Path, rows: list[dict]) -> Path:
    """Atomically overwrite review_queue.csv with the given rows.

    Write to a sibling .tmp, fsync, then replace() -- POSIX-atomic. A crash
    mid-write previously could leave the operator's primary artifact half-
    written; now either the new CSV lands fully or the previous CSV stays
    intact.
    """
    exports_dir.mkdir(parents=True, exist_ok=True)
    out_path = exports_dir / "review_queue.csv"
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
        fh.flush()
        try:
            import os as _os
            _os.fsync(fh.fileno())
        except OSError as exc:
            # Don't swallow silently: an fsync failure here usually means
            # disk-full or permission trouble, and the operator deserves to
            # see it. The atomic rename still happens; we just surface that
            # durability is not guaranteed on this filesystem write.
            print(
                f"[csv_export] WARN: fsync({tmp_path.name}) failed: {exc}. "
                f"CSV was written but may not be flushed to disk yet."
            )
    tmp_path.replace(out_path)
    return out_path
