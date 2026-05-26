"""Per-workspace SQLite schema and engine handling.

Schema mirrors the PROJECT_BRIEF SQLite schema exactly. Defined with SQLAlchemy
Core Tables so a fresh workspace db is created on first use; later sessions
thicken usage but never need a migration.
"""
from __future__ import annotations

import pathlib

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    event,
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
    # Issue #19: optional pipeline-spanning batch id. Operators that
    # want lineage ("every row this Tuesday's pipeline pass touched")
    # mint a batch via scripts/new_pipeline_batch.py and pass
    # --pipeline-batch <id> to each subsequent stage. Stages without
    # the flag leave this NULL; nothing forces a batch.
    Column("pipeline_batch_id", Text),
    # status.py orders by (workspace, stage, started_at desc) for "last run per
    # stage"; this index keeps that query bounded as runs grow into the 1000s.
    Index("ix_runs_workspace_stage_started", "workspace", "stage", "started_at"),
    Index("ix_runs_pipeline_batch_id", "pipeline_batch_id"),
)


# Issue #19: pipeline-spanning batch lineage. Optional. An operator who
# wants "every row this Tuesday's run touched" mints a batch row via
# scripts/new_pipeline_batch.py, then passes --pipeline-batch <id> to
# each stage. Every stage's `runs` row stamps pipeline_batch_id; future
# slices will extend the stamp to source_snapshots, signals,
# deal_attributions, etc. so the full sources → drafts → syncs →
# outcomes join is one query.
pipeline_batches = Table(
    "pipeline_batches", metadata,
    Column("batch_id", Text, primary_key=True),  # hex string from secrets.token_hex
    Column("workspace", Text, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("completed_at", DateTime),
    Column("operator", Text),         # $USER fallback
    Column("notes", Text),            # operator-supplied reason / context
    Index("ix_pipeline_batches_workspace", "workspace"),
    Index("ix_pipeline_batches_started_at", "started_at"),
)

run_errors = Table(
    "run_errors", metadata,
    Column("error_id", Integer, primary_key=True, autoincrement=True),
    # CASCADE: when a run row is deleted, its errors go with it. Operators
    # almost never delete runs, but in tests / a manual rebuild this keeps
    # run_errors from accumulating orphan rows.
    Column("run_id", Integer, ForeignKey("runs.run_id", ondelete="CASCADE")),
    Column("record_id", Text),
    Column("error_type", Text),
    Column("error_message", Text),
    Column("occurred_at", DateTime),
    Index("ix_run_errors_run_id", "run_id"),
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
    Index("ix_force_refresh_log_partner_id", "partner_id"),
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
    # Legacy: `; `-delimited list of source URLs (pre-Slice-18b shape).
    # Kept for backward-compatibility reads; no current code path
    # consumes it. New writers populate `source_ids` instead -- a JSON
    # list of integer source_id values into the canonical `sources`
    # registry. Operator audit goes via the JOIN.
    Column("source_urls", Text),
    # Slice 18b follow-up (#18): JSON list of source_id values that
    # contributed to this fund's enrichment. Populated by
    # core.fund_enrichment.build_fund_update_values; backfilled from
    # `source_urls` by m004_backfill_funds_source_ids.
    Column("source_ids", Text),
    Column("last_updated", DateTime),
    # Batch 32 (#742): TRUE when Stage 3 created the row from an
    # announcement before Stage 2 enriched the fund. Distinguishes
    # "fund we discovered via deal flow but haven't researched yet"
    # from "fund we've enriched + scored". Stage 6 round_fit can
    # de-emphasize provisional funds; the operator can promote one
    # via scripts/promote_provisional.py once enriched.
    Column("is_provisional", Boolean, default=False),
    # Batch 18 (#675): UNIQUE on attio_record_id. SQLite UNIQUE indexes
    # tolerate multiple NULLs, so this only constrains rows that have
    # actually been synced. Catches the duplicate-create-after-timeout
    # case at the DB layer.
    Index(
        "ux_funds_attio_record_id", "attio_record_id", unique=True,
    ),
)

partners = Table(
    "partners", metadata,
    Column("partner_id", Text, primary_key=True),
    Column("attio_record_id", Text),
    # No CASCADE on partners.fund_id: removing a fund shouldn't silently
    # nuke its partner rows. Just declare the relationship.
    Column("fund_id", Text, ForeignKey("funds.fund_id")),
    # Batch 26 (#441, #684): per-partner do-not-contact flag. Stage 6
    # treats this as a major_kill and Stage 7 routes to outreach_status=
    # do_not_contact. Distinct from warm_path_available because warm-path
    # is "use the warm channel"; do_not_contact is "use no channel".
    Column("do_not_contact", Boolean, default=False),
    Column("do_not_contact_reason", Text),
    # Slice 15: DNC audit metadata. WHEN the flag was set, BY whom,
    # and WHERE the decision came from so the operator can trace why
    # a partner is suppressed long after the original action.
    # Values for do_not_contact_source: manual | attio | gmail | csv
    Column("do_not_contact_set_at", DateTime),
    Column("do_not_contact_set_by", Text),
    Column("do_not_contact_source", Text),
    # Batch 32 (#741): TRUE when Stage 3 created the row from an
    # announcement that named the partner, BEFORE Stage 2 enrichment
    # confirms them on the team page. Stage 6 should treat
    # employment_status='uncertain' partners more cautiously; the
    # provisional flag is a stronger signal that the row needs Stage 2
    # follow-up.
    Column("is_provisional", Boolean, default=False),
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
    # Slice 9: deliverability verification state for the email above.
    # Values: unknown | valid | risky | invalid
    # Default is `unknown`; set manually via
    # scripts/set_email_verification.py or by a future Apollo import.
    Column("email_verification_status", Text, default="unknown"),
    # Slice 7: cold-outreach relationship state. Drives suppression
    # in the approval gate + Stage 6 recommendation gate. Hydrated
    # automatically by outcome ingestion (see
    # core/outcomes/persistence.py); manually overridable via
    # scripts/set_relationship.py.
    # Values:
    #   none                 -- default; no prior interaction
    #   known                -- on the operator's radar, no outreach yet
    #   contacted            -- sent outreach
    #   active_conversation  -- replied + still talking
    #   passed               -- declined (with optional cooldown window)
    #   invested             -- terminal positive outcome
    #   do_not_contact       -- terminal negative; pairs with do_not_contact column
    Column("relationship_status", Text, default="none"),
    Column("last_contacted_at", DateTime),
    Column("last_reply_at", DateTime),
    Column("last_meeting_at", DateTime),
    Column("last_outcome", Text),
    # Where the most recent outcome-derived state came from.
    # Values: manual | attio | gmail | csv
    Column("outcome_source", Text),
    Column("owner_notes", Text),
    # When relationship_status was last refreshed (auto or manual).
    Column("relationship_updated_at", DateTime),
    # Stage 4 writes the LLM-derived partial reachability score + JSON evidence.
    # Stage 6 combines this with deterministic checks to produce the final
    # cold_reachability_score in partner_score_summaries.
    Column("cold_reachability_partial_score", Float),
    Column("cold_reachability_partial_evidence", Text),
    Column("last_updated", DateTime),
    Index("ix_partners_fund_id", "fund_id"),
    # Batch 18 (#674): UNIQUE on attio_record_id. Same NULL semantics as
    # funds -- only constrains rows that have actually been synced.
    Index(
        "ux_partners_attio_record_id", "attio_record_id", unique=True,
    ),
)

# Slice 18b (REFACTOR_PLAN item 21): normalize source identity.
# Today source_url is duplicated as a TEXT column on
# source_snapshots, signals, deal_attributions, ambiguous_matches,
# and funds.source_urls (JSON list). A typo in one of the writers
# produces "two different source_urls that mean the same thing" with
# no easy way to detect it. This table is the canonical registry --
# one row per unique URL, stable source_id PK -- and over the next
# slices the loose columns migrate to FKs that point here.
#
# This slice only WIRES UP the registry + adds source_id to
# source_snapshots. Other tables keep their loose source_url for now;
# future migrations move them over (the migration system landed in
# Slice 16 makes that safe).
sources = Table(
    "sources", metadata,
    Column("source_id", Integer, primary_key=True, autoincrement=True),
    Column("source_url", Text, nullable=False),
    # source_type names the kind of feed/page this URL is. Examples:
    #   funding_announcement_feed | fund_team_page |
    #   partner_content | press_release | rss_seed | manual
    # NULL when unknown -- backfill stamps NULL on rows we can't
    # reliably categorize.
    Column("source_type", Text),
    Column("first_seen_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    # JSON metadata for future per-source state (rate-limit budget,
    # robots.txt parse, etc.); NULL today.
    Column("metadata", Text),
    # UNIQUE on source_url enforces "one canonical row per URL". A
    # second upsert of the same URL bumps last_seen_at on the
    # existing row instead of creating a duplicate.
    Index("ux_sources_source_url", "source_url", unique=True),
)


source_snapshots = Table(
    "source_snapshots", metadata,
    Column("snapshot_id", Integer, primary_key=True, autoincrement=True),
    Column("source_url", Text, nullable=False),
    # Slice 18b: FK to the canonical sources registry. Populated by
    # core.sources.upsert_source() inside source_fetch / direct
    # writers. NULL on legacy rows backfill hasn't reached yet;
    # readers should treat NULL as "use source_url string fallback".
    Column("source_id", Integer, ForeignKey("sources.source_id")),
    # Batch 29 (#326): final_url after redirect resolution. The verifier
    # and any future re-fetch step should hit final_url; source_url is
    # kept for traceability ("what did the operator originally configure").
    Column("final_url", Text),
    Column("fetched_at", DateTime, nullable=False),
    Column("http_status", Integer),
    Column("content_hash", Text),
    Column("extracted_text", Text),
    Column("fetched_during_stage", Text),
    # Stage 3 dedups by (source_url, content_hash) in application code. The
    # unique index makes that contract enforceable: a future caller can't
    # accidentally re-insert the same content. NULL content_hash is allowed
    # for pre-hash rows (multiple NULLs coexist under SQLite UNIQUE semantics).
    Index(
        "ux_source_snapshots_url_hash",
        "source_url", "content_hash",
        unique=True,
    ),
    Index("ix_source_snapshots_source_url", "source_url"),
    Index("ix_source_snapshots_source_id", "source_id"),
)

signals = Table(
    "signals", metadata,
    Column("signal_id", Integer, primary_key=True, autoincrement=True),
    # Signals are evidence -- do not cascade-delete with the partner. If a
    # partner is removed, the signals stay as historical record (operator
    # can audit "we used to think axis_3 mattered, here's the evidence").
    Column("partner_id", Text, ForeignKey("partners.partner_id")),
    Column("snapshot_id", Integer, ForeignKey("source_snapshots.snapshot_id")),
    Column("source_type", Text),
    Column("source_url", Text, nullable=False),
    # Slice 18b follow-up (#18): FK to the canonical sources registry.
    # source_url stays as a fallback for legacy callers; source_id is
    # populated by every new writer + backfilled by m003.
    Column("source_id", Integer, ForeignKey("sources.source_id")),
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
    Index("ix_signals_partner_id", "partner_id"),
    # Stage 6/7 filter "verified=1 AND signal_quality_score>=2" repeatedly;
    # this composite index keeps that bounded on workspaces with many signals.
    Index("ix_signals_verified_quality", "verified", "signal_quality_score"),
)

deal_attributions = Table(
    "deal_attributions", metadata,
    Column("deal_id", Integer, primary_key=True, autoincrement=True),
    Column("company", Text),
    Column("round_type", Text),
    Column("round_size_usd", Integer),
    Column("announcement_date", Date),
    Column("lead_fund_id", Text, ForeignKey("funds.fund_id")),
    Column("attributed_partner_id", Text, ForeignKey("partners.partner_id")),
    Column("source_url", Text),
    # Slice 18b follow-up (#18): FK to the canonical sources registry.
    # source_url stays for back-compat readers; m003 backfills source_id.
    Column("source_id", Integer, ForeignKey("sources.source_id")),
    # Sector tags persisted from the Stage 3 LLM output (JSON list).
    # Surfaced by Stage 6 round_fit for recent_relevant_deals scoring.
    Column("sector_tags", Text),
    Column("captured_at", DateTime),
    # Batch 32 (#744/#745/#746/#747/#749): raw names + confidence +
    # snapshot link preserved so a future re-run can backfill when a
    # previously-unresolved name now matches a known partner/fund, AND
    # the operator can audit why a particular attribution was made.
    Column("raw_lead_investor", Text),
    Column("raw_attributed_partners", Text),  # JSON list of names+funds
    # Batch 32: optional LLM-supplied confidence (0.0 - 1.0) or fuzzy-
    # match score for the lead fund. Useful for filtering low-confidence
    # attributions out of Stage 6 round_fit later.
    Column("match_confidence", Float),
    # Slice 6: honest attribution status. Stage 3 sets this when it
    # persists the row. Stage 6 scoring only counts confirmed +
    # strong likely toward lead_likelihood; ambiguous goes to the
    # review queue and contributes nothing to scoring; rejected is
    # never counted.
    # Values: confirmed | likely | ambiguous | rejected | unmatched
    Column("match_status", Text, default="unmatched"),
    # Slice 6: how the match was made. Drives operator audit + lets
    # Stage 6 weight matches by reliability (exact > domain >
    # fund_name fuzzy > partner_name fuzzy > llm-only).
    # Values: exact | domain | fund_name | partner_name | llm | manual
    Column("matched_by", Text),
    # Slice 6: human review trail. NULL when no review happened; set
    # by scripts/review_attribution.py when an operator confirms /
    # rejects an ambiguous row.
    Column("review_status", Text),  # confirmed | rejected | null
    Column("reviewed_by", Text),
    Column("reviewed_at", DateTime),
    Column("snapshot_id", Integer, ForeignKey("source_snapshots.snapshot_id")),
    Index("ix_deal_attributions_lead_fund_id", "lead_fund_id"),
    Index("ix_deal_attributions_attributed_partner_id", "attributed_partner_id"),
    Index("ix_deal_attributions_match_status", "match_status"),
)


# Slice 6: generic review queue. One row per item that needs human
# attention. `kind` discriminates the item type so a future UI / CLI
# can hydrate kind-specific context from the JSON blob without
# proliferating per-kind tables.
#
# kind values (extensible):
#   - ambiguous_attribution : Stage 3 produced an ambiguous fund/
#     partner match for a deal_attributions row. target_id is the
#     deal_id (text-encoded).
#   - pending_approval      : reserved for future use (mirrors the
#     dedicated draft_approvals chain but lets a single review UI
#     show both kinds).
review_items = Table(
    "review_items", metadata,
    Column("review_id", Integer, primary_key=True, autoincrement=True),
    Column("kind", Text, nullable=False),
    # Foreign-id of the underlying row (deal_id, draft_id, etc.).
    # Stored as text so future kinds can use different id types.
    Column("target_id", Text, nullable=False),
    # JSON-serialized dict with kind-specific context the reviewer
    # needs to make the decision. E.g. for ambiguous_attribution:
    # {candidates: [{fund_id, name, score}], raw_lead_investor: ...}
    Column("context", Text),
    # pending | resolved | dismissed
    Column("status", Text, default="pending"),
    Column("resolved_by", Text),
    Column("resolved_at", DateTime),
    Column("resolution_notes", Text),
    Column("created_at", DateTime),
    Index("ix_review_items_kind_status", "kind", "status"),
    Index("ix_review_items_target", "kind", "target_id"),
)

scores = Table(
    "scores", metadata,
    # CASCADE: removing a partner removes their per-axis scores.
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("axis_id", Text, primary_key=True),
    Column("score", Float),
    Column("supporting_signal_ids", Text),
    Column("confidence", Text),
    # NOTE: scored_at is in the PK by historical accident -- a different
    # timestamp creates a new row. Stage 6 deletes-then-inserts per partner
    # so duplicates don't accumulate; leaving the PK shape unchanged to
    # avoid a destructive migration on operator dbs. Documented so a future
    # change can drop it intentionally.
    Column("scored_at", DateTime, primary_key=True),
)

partner_score_summaries = Table(
    "partner_score_summaries", metadata,
    # CASCADE: removing a partner removes their summary row.
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
        primary_key=True,
    ),
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
    # Slice 10: round-building model adds an investor role + a
    # confidence band so batch selection can balance roles instead
    # of pure top-N. Set by core/scoring/round_building.py.
    # investor_role values:
    #   potential_lead | strong_co_investor | strategic_specialist
    #   | credible_signal_investor | wildcard_high_conviction_fit
    #   | low_priority
    Column("investor_role", Text),
    # confidence_band values:
    #   high | medium | low | insufficient_evidence
    Column("confidence_band", Text),
    # Stage 7 orders by send_now_priority DESC LIMIT N every run.
    Index("ix_pss_send_now_priority", "send_now_priority"),
    Index("ix_pss_investor_role", "investor_role"),
)

email_drafts = Table(
    "email_drafts", metadata,
    Column("draft_id", Integer, primary_key=True, autoincrement=True),
    # CASCADE: drafts belong to the partner; removing the partner removes
    # their drafts.
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
    ),
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
    # Slice 11: founder-conviction-to-partner bridge. Built at draft
    # generation time from company.yaml's founder_conviction block +
    # this partner's verified signals. The bridge is what makes the
    # email evidence-backed: it ties the founder's non-obvious belief
    # to a specific partner-held belief inferred from a partner
    # signal. Review surface exposes these fields so the operator
    # can audit the connection before approving.
    Column("bridge_founder_claim", Text),
    Column("bridge_partner_belief", Text),
    Column("bridge_partner_evidence", Text),
    Column("bridge_sentence", Text),
    # low | medium | high -- how risky is the factual claim that
    # connects founder_claim to partner_belief? Operator review
    # focus.
    Column("bridge_factual_risk", Text),
    # low | medium | high -- how confident are we the partner
    # actually holds this belief, given the evidence shape?
    Column("bridge_confidence", Text),
    # Slice 1: approval state-machine pointer. Always seeded as
    # 'needs_review' on insert -- only a human action moves it to
    # 'approved_to_send'. Gmail / Attio / CSV-export readers filter
    # on approval_status='approved_to_send'.
    # Values: needs_review | approved_to_send | rejected
    #       | stale_after_approval | sent
    Column("approval_status", Text, default="needs_review"),
    # sha256 of the canonical (subject + body) at draft time. When an
    # approved draft's score / evidence changes after approval, a new
    # draft is regenerated and the hash differs -- triggers
    # stale_after_approval automatically. Full append-only history
    # lives in draft_approvals.
    Column("draft_hash", Text),
    # Slice 17: immutable history. Stage 7's re-run used to
    # `DELETE FROM email_drafts WHERE partner_id = ?` then INSERT a
    # fresh set -- a buggy run could wipe a good prior batch
    # irrecoverably. The new shape SUPERSEDES the prior rows
    # (sets superseded_at + clears is_recommended + state-machine-
    # stales any approved draft) and INSERTS the new ones with
    # version + 1. Operator audit can replay any prior generation by
    # filtering on partner_id + version.
    Column("version", Integer, default=1),
    Column("superseded_at", DateTime),
    Index("ix_email_drafts_partner_id", "partner_id"),
    Index("ix_email_drafts_batch_id", "batch_id"),
    # Approval queue / Gmail send queries filter on approval_status;
    # keep that fast.
    Index("ix_email_drafts_approval_status", "approval_status"),
)


# Slice 18a (REFACTOR_PLAN item 19): normalize manual_override reasons
# into an append-only event log. The legacy
# `partner_score_summaries.manual_override_reason` packed namespaced
# reasons into one TEXT field ("score: bumped; rec: yes; warm: ...");
# this table captures each operator action as its own row so audits
# can answer "who set what when, with what justification" without
# spelunking a string.
#
# kind values:
#   score   -- manual score override set/cleared
#   rec     -- manual recommendation override set/cleared
#   warm    -- warm-path flag set/cleared (Slice 7 deprecated this
#              from scoring but operators still record it for context)
#
# action values:
#   set     -- override applied with `reason`
#   clear   -- override removed (reason optional)
#
# The legacy packed string is still written by manual_override.py for
# back-compat (read paths haven't fully migrated); this table is the
# new canonical event source.
manual_override_events = Table(
    "manual_override_events", metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", Text, nullable=False),  # score | rec | warm
    Column("action", Text, nullable=False),  # set | clear
    Column("reason", Text),
    # Recommendation override carries the new value (yes/no) so the
    # event log can reconstruct the recommended state without joining
    # to partner_score_summaries. NULL for score / warm events.
    Column("new_value", Text),
    Column("actor", Text, nullable=False),
    Column("at", DateTime, nullable=False),
    Index("ix_moe_partner_id", "partner_id"),
    Index("ix_moe_partner_kind", "partner_id", "kind"),
)


# Slice 1: append-only event log of every approval action against a
# draft. The latest row's event_type matches email_drafts.approval_status
# (denormalized for fast filtering); this table preserves WHO, WHEN,
# WHY, and WHAT BODY HASH for audit + dispute / forensics.
#
# event_type values mirror the state-machine transitions:
#   - needs_review        : system, on draft insert
#   - approved_to_send    : human, via approve_draft CLI / UI
#   - rejected            : human, via reject_draft CLI / UI
#   - stale_after_approval: system, when a material change invalidates
#                            a prior approval (score / evidence / body
#                            regeneration -> draft_hash mismatch)
#   - sent                : system, after Gmail / Attio confirms send
draft_approvals = Table(
    "draft_approvals", metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "draft_id", Integer,
        ForeignKey("email_drafts.draft_id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Denormalized so review queue can filter on partner without a join.
    Column("partner_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    # 'system' for auto-generated events; an operator identifier
    # (resolved from $USER / $USERNAME / explicit --approved-by) for
    # human actions.
    Column("actor", Text, nullable=False),
    Column("at", DateTime, nullable=False),
    # Snapshot of the (subject + body) hash at the moment of the
    # event. Lets us prove "this exact body was approved" even after
    # later edits.
    Column("draft_hash", Text),
    # Optional operator note (approval reasoning, rejection reason,
    # stale trigger detail).
    Column("notes", Text),
    # Override durability (PR #7 follow-up review finding): when an
    # approval used --override-blockers, the SOFT blockers the operator
    # acknowledged are persisted here as a JSON list. Downstream gates
    # (export_send_queue, Gmail, Attio, check_ready) consult this row
    # so an overridden draft isn't immediately re-flagged as stale.
    # NULL on non-override approvals.
    Column("overridden_blockers", Text),
    Index("ix_draft_approvals_draft_id", "draft_id"),
    Index("ix_draft_approvals_partner_id", "partner_id"),
    Index("ix_draft_approvals_event_type", "event_type"),
)

followup_drafts = Table(
    "followup_drafts", metadata,
    Column("followup_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
    ),
    # Batch 23 (#473): join to email_drafts.batch_id so a followup row
    # can be traced back to the Stage 7 batch it was generated for.
    Column("batch_id", Text),
    Column("body", Text),
    Column("generated_at", DateTime),
    Column("pushed_to_attio_at", DateTime),
    # Slice 17 follow-up (#17): immutable history matching email_drafts.
    # Stage 7's per-partner DELETE becomes UPDATE-supersede; new row
    # gets version+1. Operator audit can recover any prior generation.
    Column("version", Integer, default=1),
    Column("superseded_at", DateTime),
    Index("ix_followup_drafts_partner_id", "partner_id"),
    Index("ix_followup_drafts_batch_id", "batch_id"),
)

deck_request_responses = Table(
    "deck_request_responses", metadata,
    Column("response_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
    ),
    # Batch 23 (#474): same batch_id link as followup_drafts.
    Column("batch_id", Text),
    Column("body", Text),
    Column("generated_at", DateTime),
    Column("pushed_to_attio_at", DateTime),
    # Slice 17 follow-up (#17): immutable history.
    Column("version", Integer, default=1),
    Column("superseded_at", DateTime),
    Index("ix_deck_request_responses_partner_id", "partner_id"),
    Index("ix_deck_request_responses_batch_id", "batch_id"),
)

meeting_prep_artifacts = Table(
    # Build Session 12: cached LLM output for the objection-map and
    # framing-brief builders. Keyed on (partner_id, artifact_type,
    # signal_set_hash) -- re-running prep_brief.py against unchanged
    # signals is free (zero LLM calls); a new verified signal flips
    # the hash and forces a regenerate.
    "meeting_prep_artifacts", metadata,
    Column("artifact_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "partner_id", Text,
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
    ),
    # "objection_map" | "framing_brief". Free-text column because
    # adding a third artifact type later shouldn't need a migration.
    Column("artifact_type", Text, nullable=False),
    # SHA-256 of the sorted, verified, quality>=2 signal_ids for this
    # partner at build time. Cache hit when this matches the current
    # set; miss otherwise. Same content-hash idiom as
    # source_snapshots.content_hash.
    Column("signal_set_hash", Text, nullable=False),
    # Full pydantic-validated JSON output, stringified. The renderer
    # parses it back; the operator can read it raw for debugging.
    Column("payload_json", Text, nullable=False),
    # True when the underlying builder returned insufficient_evidence
    # -- cached separately so renderers can short-circuit without
    # parsing the payload.
    Column("insufficient_evidence", Boolean, nullable=False),
    Column("model_used", Text),
    Column("generated_at", DateTime),
    # Build Session 13: Drive auto-push columns. drive_doc_id is the
    # Google Doc id created on first successful upload; drive_doc_url
    # is its webViewLink (cached so renderers can deep-link without a
    # follow-up API call). Both stay NULL when Drive isn't connected,
    # the upload failed, or the operator hasn't connected Google yet.
    Column("drive_doc_id", Text),
    Column("drive_doc_url", Text),
    Column("drive_pushed_at", DateTime),
    # Index on (partner_id, artifact_type) so the lookup is O(1)
    # without scanning every row.
    Index(
        "ix_meeting_prep_artifacts_partner_type",
        "partner_id", "artifact_type",
    ),
)


batch_qa_reports = Table(
    "batch_qa_reports", metadata,
    Column("report_id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", Text),
    Column("batch_size", Integer),
    # Batch 23 (#367/#467): batch_size historically stored the number of
    # draft ROWS (partners x variants). Add an explicit per-partner count
    # so the operator can reconcile "I expected 25 partners; the QA
    # report shows 50 drafts" without having to know the variants-per-
    # partner ratio.
    Column("batch_partner_count", Integer),
    Column("strategy_distribution", Text),
    Column("similarity_failures", Integer),
    Column("template_smell_high_count", Integer),
    Column("raise_reference_missing_count", Integer),
    Column("passed", Boolean),
    Column("failure_reasons", Text),
    Column("generated_at", DateTime),
    Index("ix_batch_qa_reports_batch_id", "batch_id"),
)

# Batch 34 (#757/#758/#759/#760): operator-supplied corrections /
# rejections for individual Stage 3 attributions. Keyed by source_url
# (one announcement => one override). Persisted across Stage 3 re-runs
# so the LLM can't silently reintroduce a wrong attribution the operator
# previously rejected.
#
# action='reject' -> Stage 3 leaves a SKELETON row only (raw names but
#                    NULL lead_fund_id + NULL partner attribution)
# action='set'    -> Stage 3 uses the supplied lead_fund_id /
#                    attributed_partner_id verbatim, ignoring the LLM
# action='note'   -> annotation only; Stage 3 still resolves via the
#                    LLM but the note is surfaced for audit
deal_attribution_overrides = Table(
    "deal_attribution_overrides", metadata,
    Column("override_id", Integer, primary_key=True, autoincrement=True),
    Column("source_url", Text, nullable=False),
    Column("action", Text, nullable=False),  # 'reject' | 'set' | 'note'
    Column("lead_fund_id", Text),
    Column("attributed_partner_id", Text),
    Column("reason", Text),
    Column("created_by", Text),
    Column("created_at", DateTime),
    Index(
        "ux_deal_attribution_overrides_source_url",
        "source_url", unique=True,
    ),
)


# Batch 33 (#341/#342/#737/#738): record fuzzy matches that were
# ambiguous (multiple candidates close to the best). Lets the operator
# spot wrong attributions ("Foundry North" matched "Foundry NorthEast"
# at 0.86 -- but Foundry NorthWest scored 0.85") via
# scripts/list_ambiguous_matches.py and resolve them via
# scripts/resolve_ambiguous_match.py.
ambiguous_matches = Table(
    "ambiguous_matches", metadata,
    Column("match_id", Integer, primary_key=True, autoincrement=True),
    Column("entity_type", Text),          # 'fund' or 'partner'
    Column("raw_name", Text),
    Column("source_url", Text),
    # Slice 18b follow-up (#18): FK to the canonical sources registry.
    Column("source_id", Integer, ForeignKey("sources.source_id")),
    Column("candidates", Text),           # JSON list of {id, name, score}
    Column("chosen_id", Text),            # the auto-picked id (may be NULL)
    Column("chosen_score", Float),
    Column("resolved_id", Text),          # operator-supplied final id
    Column("resolved_at", DateTime),
    Column("resolved_by", Text),
    Column("resolution_note", Text),
    Column("captured_at", DateTime),
    Index("ix_ambiguous_matches_entity_type", "entity_type"),
    Index("ix_ambiguous_matches_resolved", "resolved_id"),
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
    Index("ix_attio_sync_log_local_id", "local_id"),
)

outcomes = Table(
    "outcomes", metadata,
    Column("outcome_id", Integer, primary_key=True, autoincrement=True),
    # Outcomes are events -- do not cascade. If a partner is removed, the
    # outcome history stays for audit.
    Column("partner_id", Text, ForeignKey("partners.partner_id")),
    Column("outreach_status", Text),
    Column("reply_type", Text),
    Column("meeting_booked", Boolean),
    Column("meeting_date", Date),
    Column("meeting_outcome", Text),
    Column("synced_from_attio_at", DateTime),
    # Distinguishes where the row came from: "attio" (outcome_sync),
    # "manual" (record_outcome / classify_reply), or "fixture" (seeded by
    # monthly_learning_report --seed-fixture-outcomes). Lets the learning
    # report refuse to train on fixture rows if it later runs in a real
    # workspace that was scaffolded from a fixture seed.
    Column("source", Text),
    # Batch 31 (#522): external event ID for cross-source dedup. Attio
    # rows can be keyed by the Attio activity-event id; manual rows can
    # use a stable hash. A future outcome_sync rewrite can ON CONFLICT
    # on this column to avoid duplicate ingestion across cron retries.
    Column("external_event_id", Text),
    Index("ix_outcomes_partner_id", "partner_id"),
    Index("ix_outcomes_source", "source"),
    Index(
        "ux_outcomes_external_event_id",
        "external_event_id", unique=True,
    ),
)

calibration_cohorts = Table(
    "calibration_cohorts", metadata,
    Column("cohort_id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime, nullable=False),
    Column("partner_ids", Text, nullable=False),  # JSON list
    Column("outcome", Text),  # "green", "yellow", "red", or NULL while in flight
    Column("reason", Text),
    Column("completed_at", DateTime),
    # Stage 7's Gate 5.5 query: WHERE outcome='green' AND completed_at >= cutoff
    # ORDER BY completed_at DESC LIMIT 1
    Index("ix_calibration_cohorts_outcome_completed", "outcome", "completed_at"),
)

learning_runs = Table(
    "learning_runs", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("generated_at", DateTime, nullable=False),
    Column("terminal_outcomes", Integer),
    Column("excluded_pending", Integer),
    Column("excluded_no_draft", Integer),
    Column("strategy_rates", Text),        # JSON: {strategy -> reply_rate}
    Column("reachability_rates", Text),    # JSON: {bucket -> reply_rate}
    Column("variance_rates", Text),        # JSON: {bucket -> reply_rate}
    Column("suggestions_written", Integer),
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
    # Batch 15 (#296): who approved and why. The reason is operator-
    # supplied at apply time so the rationale lands alongside the
    # generated `reason` column from the learning report.
    Column("approved_by", Text),
    Column("approval_reason", Text),
    Index("ix_axis_weight_suggestions_approved", "approved"),
)


def _enable_sqlite_foreign_keys(dbapi_conn, _conn_record) -> None:
    """SQLite ships with FK checking OFF by default per connection. Without
    this listener, ForeignKey + ondelete=CASCADE declared on the Tables above
    are inert (the schema records the relationship but the engine never
    enforces it). Set the pragma at every connect so cascades fire and
    orphan inserts are rejected."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON")
    cur.close()


def get_engine(db_url: str) -> Engine:
    """Create the engine and ensure all tables exist (idempotent).

    Also ensures the SQLite parent directory exists; Workspace.__init__ no
    longer mkdirs at load time, so the first write-stage's call to
    get_engine is where data_dir actually gets created.
    """
    if db_url.startswith("sqlite:///"):
        db_path = pathlib.Path(db_url[len("sqlite:///"):])
        db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(db_url, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    # Slice 16: detect freshness BEFORE metadata.create_all runs --
    # afterwards every workspace has user tables and we can't tell
    # "brand-new (latest schema, stamp migrations as applied)" from
    # "pre-existing (run real migrations)". Inspect with a temporary
    # introspector since `inspect(engine)` reuses state per-engine.
    from sqlalchemy import inspect as _inspect
    _existing_tables = set(_inspect(engine).get_table_names())
    _existing_tables.discard("sqlite_sequence")  # SQLite internal
    was_empty_before_create_all = not _existing_tables
    metadata.create_all(engine)
    # Defensive: a pre-existing SQLite db may lack columns added in later
    # sessions. metadata.create_all does not ALTER. Sync any drift here.
    # NOTE: it also doesn't ALTER to add FK constraints -- ForeignKey
    # declarations only apply to freshly-created tables. Operator dbs from
    # earlier sessions retain their FK-less schema until the table is
    # dropped + recreated. Indexes added here ARE picked up on existing
    # tables because metadata.create_all emits CREATE INDEX IF NOT EXISTS.
    _sync_columns_with_metadata(engine)
    # Slice 16: real migration system runs after additive drift sync.
    # `_sync_columns_with_metadata` handles the easy ALTER-ADD-COLUMN
    # case; this picks up renames / drops / backfills / index changes
    # that can't be derived from the SQLAlchemy metadata alone.
    from core.migrations import apply_pending_migrations
    apply_pending_migrations(
        engine, was_empty_before_create_all=was_empty_before_create_all,
    )
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
    """Idempotent insert-or-update keyed on the table's primary-key columns.

    SQLite's ON CONFLICT requires the index_elements to be the actual PK or
    a unique index. We assert pk_cols is exactly the table's PK so future
    callers can't quietly accept non-unique columns and get runtime failures
    only when conflicts happen.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    actual_pk = {c.name for c in table.primary_key.columns}
    requested = set(pk_cols)
    if requested != actual_pk:
        raise ValueError(
            f"upsert(): pk_cols {sorted(requested)} does not match "
            f"{table.name}.primary_key {sorted(actual_pk)}. "
            f"ON CONFLICT only works against the actual PK / unique constraint."
        )
    stmt = sqlite_insert(table).values(**values)
    update_cols = {c: stmt.excluded[c] for c in values if c not in pk_cols}
    if update_cols:
        stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    conn.execute(stmt)
