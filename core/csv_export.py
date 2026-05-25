"""CSV exports for the cold-outreach review + send workflow.

Two distinct CSVs (Slice 1):

  - review_queue.csv : every draft Stage 7 produced, regardless of
    approval state. The operator reads this to decide what to
    approve / reject / edit. Includes needs_review + qa_failed +
    rejected + (rarely) stale_after_approval rows. Overwritten on
    each Stage 7 run.

  - send_queue.csv : ONLY drafts in approval_status='approved_to_send'.
    This is what an external sender (manual paste, Gmail import,
    etc.) actually consumes. Stage 7 does NOT write this; it is
    written by scripts/export_send_queue.py or similar after the
    operator has approved drafts. Never auto-populated.

Both CSVs use atomic write (.tmp + replace) so a mid-write crash
leaves the prior file intact.
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


def _atomic_write_csv(out_path: Path, columns: list[str],
                      rows: list[dict]) -> None:
    """Atomic CSV write: temp file + fsync + replace. Shared by both
    review_queue and send_queue writers."""
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=columns, extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
        fh.flush()
        try:
            import os as _os
            _os.fsync(fh.fileno())
        except OSError as exc:
            print(
                f"[csv_export] WARN: fsync({tmp_path.name}) failed: "
                f"{exc}. CSV was written but may not be flushed to "
                f"disk yet."
            )
    tmp_path.replace(out_path)


def write_review_queue(exports_dir: Path, rows: list[dict]) -> Path:
    """Atomically overwrite review_queue.csv with the given rows.

    The review queue includes drafts in every state -- needs_review,
    qa_failed, rejected, stale_after_approval -- so the operator can
    see the full picture. The send_queue.csv (separate file) is the
    filtered view that consumers should ACTUALLY send from.
    """
    exports_dir.mkdir(parents=True, exist_ok=True)
    out_path = exports_dir / "review_queue.csv"
    _atomic_write_csv(out_path, CSV_COLUMNS, rows)
    return out_path


# Send queue CSV (Slice 1): ONLY approved drafts.
# Smaller column set than the review queue because the consumer
# (manual paste / Gmail import / external sender) doesn't need the
# scoring / blocker context -- they need the recipient + subject +
# body + ids.
SEND_QUEUE_COLUMNS: list[str] = [
    "draft_id",
    "partner_id",
    "partner_name",
    "partner_email",
    "fund_name",
    "approved_at",
    "approved_by",
    "email_subject_line",
    "outreach_email_draft",
    "followup_email_draft",
    "deck_request_response",
    "draft_hash",
]


def write_send_queue(exports_dir: Path, rows: list[dict]) -> Path:
    """Atomically overwrite send_queue.csv with only-approved rows.

    The caller (scripts/export_send_queue.py) is responsible for
    filtering to approval_status='approved_to_send' -- this function
    just writes whatever it's given. Centralizing the filter in
    core.approval.persistence.approved_for_send() keeps the rule in
    one place.
    """
    exports_dir.mkdir(parents=True, exist_ok=True)
    out_path = exports_dir / "send_queue.csv"
    _atomic_write_csv(out_path, SEND_QUEUE_COLUMNS, rows)
    return out_path
