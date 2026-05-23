"""Per-workspace SQLite schema and engine handling.

Schema mirrors the PROJECT_BRIEF SQLite schema exactly. Defined with SQLAlchemy
Core Tables so a fresh workspace db is created on first use; later sessions
thicken usage but never need a migration.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("workspace", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("completed_at", DateTime),
    Column("records_processed", Integer),
    Column("records_succeeded", Integer),
    Column("records_failed", Integer),
    Column("records_skipped", Integer),
    Column("llm_calls_made", Integer),
    Column("llm_input_tokens", Integer),
    Column("llm_output_tokens", Integer),
    Column("estimated_cost_usd", Float),
    Column("elapsed_seconds", Integer),
    Column("error_summary", Text),
)

run_errors = Table(
    "run_errors", metadata,
    Column("error_id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer),
    Column("record_id", Text),
    Column("error_type", Text),
    Column("error_message", Text),
    Column("occurred_at", DateTime),
)

force_refresh_log = Table(
    "force_refresh_log", metadata,
    Column("refresh_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("field_name", Text),
    Column("old_value", Text),
    Column("new_value", Text),
    Column("reason", Text),
    Column("refreshed_at", DateTime),
)

funds = Table(
    "funds", metadata,
    Column("fund_id", Text, primary_key=True),
    Column("attio_record_id", Text),
    Column("name", Text, nullable=False),
    Column("domain", Text),
    Column("stated_thesis", Text),
    Column("stated_stage_focus", Text),
    Column("check_size_range", Text),
    Column("last_known_activity_date", Date),
    Column("is_active", Boolean),
    Column("kill_signals", Text),
    Column("source_urls", Text),
    Column("last_updated", DateTime),
)

partners = Table(
    "partners", metadata,
    Column("partner_id", Text, primary_key=True),
    Column("attio_record_id", Text),
    Column("fund_id", Text),
    Column("name", Text, nullable=False),
    Column("title", Text),
    Column("linkedin_url", Text),
    Column("twitter_handle", Text),
    Column("bio", Text),
    Column("employment_status", Text, default="uncertain"),
    Column("employment_verification_source_urls", Text),
    Column("employment_verification_date", Date),
    Column("warm_path_available", Boolean, default=None),
    Column("warm_path_contact", Text),
    # Partner email -- used by create_gmail_drafts.py. Populated manually via
    # scripts/set_partner_email.py or downstream from real enrichment.
    Column("email", Text),
    # Stage 4 writes the LLM-derived partial reachability score + JSON evidence.
    # Stage 6 combines this with deterministic checks to produce the final
    # cold_reachability_score in partner_score_summaries.
    Column("cold_reachability_partial_score", Float),
    Column("cold_reachability_partial_evidence", Text),
    Column("last_updated", DateTime),
)

source_snapshots = Table(
    "source_snapshots", metadata,
    Column("snapshot_id", Integer, primary_key=True, autoincrement=True),
    Column("source_url", Text, nullable=False),
    Column("fetched_at", DateTime, nullable=False),
    Column("http_status", Integer),
    Column("content_hash", Text),
    Column("extracted_text", Text),
    Column("fetched_during_stage", Text),
)

signals = Table(
    "signals", metadata,
    Column("signal_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("snapshot_id", Integer),
    Column("source_type", Text),
    Column("source_url", Text, nullable=False),
    Column("quoted_text", Text, nullable=False),
    Column("quote_date", Date),
    Column("axis_relevance", Text),
    Column("signal_direction", Text),
    Column("verified", Boolean, default=False),
    Column("verification_method", Text),
    Column("verification_error", Text),
    Column("signal_quality_score", Integer),
    Column("quality_reasoning", Text),
    Column("captured_at", DateTime),
)

deal_attributions = Table(
    "deal_attributions", metadata,
    Column("deal_id", Integer, primary_key=True, autoincrement=True),
    Column("company", Text),
    Column("round_type", Text),
    Column("round_size_usd", Integer),
    Column("announcement_date", Date),
    Column("lead_fund_id", Text),
    Column("attributed_partner_id", Text),
    Column("source_url", Text),
    # Sector tags persisted from the Stage 3 LLM output (JSON list).
    # Surfaced by Stage 6 round_fit for recent_relevant_deals scoring.
    Column("sector_tags", Text),
    Column("captured_at", DateTime),
)

scores = Table(
    "scores", metadata,
    Column("partner_id", Text, primary_key=True),
    Column("axis_id", Text, primary_key=True),
    Column("score", Float),
    Column("supporting_signal_ids", Text),
    Column("confidence", Text),
    Column("scored_at", DateTime, primary_key=True),
)

partner_score_summaries = Table(
    "partner_score_summaries", metadata,
    Column("partner_id", Text, primary_key=True),
    Column("composite_fit_score", Float),
    Column("axis_max_score", Float),
    Column("axis_score_variance", Float),
    Column("spiky_belief_score", Float),
    Column("score_confidence", Text),
    Column("verified_signal_count", Integer),
    Column("quality_2_plus_signal_count", Integer),
    Column("distinct_source_type_count", Integer),
    Column("most_recent_signal_date", Date),
    Column("major_kill_signal_present", Boolean),
    Column("kill_signal_summary", Text),
    Column("cold_reachability_score", Float),
    Column("round_fit_score", Float),
    Column("round_fit_reasoning", Text),
    Column("lead_likelihood_score", Float),
    Column("lead_likelihood_signals", Text),
    Column("send_now_priority", Float),
    Column("employment_status", Text),
    Column("manual_score_override", Boolean, default=False),
    Column("manual_recommended_override", Boolean, default=False),
    Column("manual_override_reason", Text),
    Column("recommended_to_send", Boolean),
    Column("recommendation_reasoning", Text),
    Column("scored_at", DateTime),
)

email_drafts = Table(
    "email_drafts", metadata,
    Column("draft_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("batch_id", Text),
    Column("strategy", Text),
    Column("subject", Text),
    Column("body", Text),
    Column("conversion_hypothesis", Text),
    Column("likely_objection", Text),
    Column("objection_preempted", Boolean),
    Column("preemption_line", Text),
    Column("template_smell", Text, default="unscored"),
    Column("qa_status", Text, default="unscored"),
    Column("regeneration_count", Integer, default=0),
    Column("is_recommended", Boolean),
    Column("generated_at", DateTime),
    Column("pushed_to_attio_at", DateTime),
    Column("written_to_csv_at", DateTime),
    # Gmail draft id once create_gmail_drafts.py has run; idempotent guard.
    Column("pushed_to_gmail_at", DateTime),
    Column("gmail_draft_id", Text),
)

followup_drafts = Table(
    "followup_drafts", metadata,
    Column("followup_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("body", Text),
    Column("generated_at", DateTime),
    Column("pushed_to_attio_at", DateTime),
)

deck_request_responses = Table(
    "deck_request_responses", metadata,
    Column("response_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("body", Text),
    Column("generated_at", DateTime),
    Column("pushed_to_attio_at", DateTime),
)

batch_qa_reports = Table(
    "batch_qa_reports", metadata,
    Column("report_id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", Text),
    Column("batch_size", Integer),
    Column("strategy_distribution", Text),
    Column("similarity_failures", Integer),
    Column("template_smell_high_count", Integer),
    Column("raise_reference_missing_count", Integer),
    Column("passed", Boolean),
    Column("failure_reasons", Text),
    Column("generated_at", DateTime),
)

attio_sync_log = Table(
    "attio_sync_log", metadata,
    Column("sync_id", Integer, primary_key=True, autoincrement=True),
    Column("object_type", Text),
    Column("local_id", Text),
    Column("attio_record_id", Text),
    Column("operation", Text),
    Column("success", Boolean),
    Column("error_message", Text),
    Column("synced_at", DateTime),
)

outcomes = Table(
    "outcomes", metadata,
    Column("outcome_id", Integer, primary_key=True, autoincrement=True),
    Column("partner_id", Text),
    Column("outreach_status", Text),
    Column("reply_type", Text),
    Column("meeting_booked", Boolean),
    Column("meeting_date", Date),
    Column("meeting_outcome", Text),
    Column("synced_from_attio_at", DateTime),
)

calibration_cohorts = Table(
    "calibration_cohorts", metadata,
    Column("cohort_id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime, nullable=False),
    Column("partner_ids", Text, nullable=False),  # JSON list
    Column("outcome", Text),  # "green", "yellow", "red", or NULL while in flight
    Column("reason", Text),
    Column("completed_at", DateTime),
)

axis_weight_suggestions = Table(
    "axis_weight_suggestions", metadata,
    Column("suggestion_id", Integer, primary_key=True, autoincrement=True),
    Column("generated_at", DateTime),
    Column("axis_id", Text),
    Column("current_weight", Float),
    Column("suggested_weight", Float),
    Column("reason", Text),
    Column("confidence", Text),
    Column("sample_size", Integer),
    Column("approved", Boolean, default=None),
    Column("approved_at", DateTime),
)


def get_engine(db_url: str) -> Engine:
    """Create the engine and ensure all tables exist (idempotent)."""
    engine = create_engine(db_url, future=True)
    metadata.create_all(engine)
    # Defensive: a pre-existing SQLite db may lack columns added in later
    # sessions. metadata.create_all does not ALTER. Sync any drift here.
    _sync_columns_with_metadata(engine)
    return engine


def _sync_columns_with_metadata(engine: Engine) -> None:
    """SQLite: ADD COLUMN for any metadata column that is missing on disk."""
    for table in metadata.sorted_tables:
        with engine.begin() as conn:
            existing = {
                row[1] for row in conn.exec_driver_sql(
                    f"PRAGMA table_info({table.name})"
                )
            }
            for col in table.columns:
                if col.name not in existing:
                    sql_type = col.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table.name} ADD COLUMN {col.name} {sql_type}"
                    )


def upsert(conn, table: Table, pk_cols: list[str], values: dict) -> None:
    """Idempotent insert-or-update keyed on the given primary-key columns."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(table).values(**values)
    update_cols = {c: stmt.excluded[c] for c in values if c not in pk_cols}
    if update_cols:
        stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    conn.execute(stmt)
