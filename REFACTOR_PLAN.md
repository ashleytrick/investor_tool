# Refactor Plan (post safety/correctness pass)

User-supplied architecture refactor roadmap. To execute AFTER the
remaining safety/correctness batches (41-43) finish. Captured here so
the plan survives context resets.

## Foundations (Best Refactors)

1. **Shared stage runner.** One wrapper that handles parse-workspace,
   preflight, banner, engine, RunLogger, llm.usage attach, failure
   accounting, exit-code policy. Every script repeats this; collapsing
   it makes loud-failure automatic and removes the recurring source of
   "stage X exits 0 on row failure" bugs.

2. **Stage result semantics.** Single policy:
   - `0` clean completion
   - `1` CLI/config usage error
   - `2` operational/data failure
   - `3` refused unsafe action
   Stages currently mix these.

3. **StageRun accounting helper.** Expose `record_success()`,
   `record_failure()`, `record_skip()`. Today every script mutates
   `run.processed/succeeded/failed/skipped` by hand → drift.

4. **CSV ingestion validators.** Stage 1, Stage 4, future imports all
   need: required columns, unknown-ID checks, duplicates, malformed
   URL/email/domain, row-level errors. One module.

5. **Shared source-fetch abstraction.** Stages 1-5 each fetch external
   URLs. Should share: retries, final URL tracking, HTTP status
   recording, content extraction, source_snapshots writes, rate limits,
   failure logging. Today Stage 2 / Stage 4 / verify all have slight
   variants.

6. **Separate fetch from extract.** Stage 2 + Stage 4 mix fetching,
   parsing, LLM prompting, DB writes, stale-state policy. Split:
   collect → fetch → snapshot → extract structured facts → DB apply →
   reconcile stale state. Each step independently testable.

7. **Business rules out of scripts.** Recommendation gates, outreach
   suppression, strategy eligibility, readiness state, Attio preserve
   behavior → `core/` modules with unit tests. Scripts only orchestrate.

8. **Domain models for pipeline artifacts.** Replace raw dicts /
   SQLAlchemy rows in business logic with typed objects:
   FundSource, PartnerContentSource, VerifiedSignal, DealAttribution,
   RecommendationDecision, DraftReadiness. Reduces silent shape drift.

9. **Real migration system.** `_sync_columns_with_metadata` works but
   the app has outgrown it. Add schema_versions table + per-version
   migration files (lightweight, SQLite-only OK).

10. **Workspace mode policy object.** Replace scattered
    `--fixtures`/`--allow-fixture-mode`/`--allow-example-domains`/
    `--allow-unknown-partner-ids` flags with one policy that stages
    consult: "fixture", "dry_run", "production". Each mode declares
    what's allowed.

## Stage-Specific Refactors

11. **Stage 2 → fund enrichment service.** `core/fund_enrichment.py`:
    page discovery / page scoring / extraction / partner reconciliation
    / demotion proposal creation.

12. **Stage 4 → partner evidence service.** `core/partner_evidence.py`:
    CSV validation / content fetch / extraction / signal upsert /
    reachability update / stale content reconciliation.

13. **Stage 6 → split scoring from persistence.**
    `core/scoring/composite.py`, `core/scoring/reachability.py`,
    `core/scoring/recommendation.py`, `core/scoring/persistence.py`.

14. **Stage 7 → split generation from QA.** Re-running QA without
    regenerating drafts becomes possible, and preserving prior good
    drafts becomes the natural shape, not a special case.

15. **Stage 8 → split payload from network.** Pure payload + matching
    decisions, then a thin network executor. Dry-run and tests get
    much cleaner.

16. **Outcome sync → event ingestion layer.** Long term, outcomes
    should be event-based (not "latest person record changed").
    `external_event_id` schema is already in place; build source-
    specific adapters + dedup on top.

## Data Refactors

17. **Generic `review_items` table.** Unifies the ambiguous-Stage-3-
    attribution queue, Stage 2 employment demotion queue, Stage 4
    stale reachability clears, Stage 7 draft QA review.

18. **Immutable history tables.** Instead of hard-deleting old
    drafts/scores, preserve generations and mark the active one.
    Closes off a class of "good prior batch wiped by bad re-run" bugs.

19. **Normalize manual override reasons.** Packed reason strings →
    `manual_override_events` table.

20. **Normalize warm-path contacts.** Structured fields: contact name,
    email, relationship, intro status, notes, last updated.

21. **Normalize source identity.** One `sources` registry instead of
    every stage loosely storing source_url + source_type + snapshot_id.

22. **Batch IDs across the entire pipeline.** Stage 7 has `batch_id`;
    extend so a single run/batch ID connects sources → signals →
    scores → drafts → syncs → outcomes.

## Testing Refactors

23. **Split the giant smoke test file.**
    `tests/test_stage1_sources.py`, `_stage2_enrichment.py`,
    `_stage4_signals.py`, `_stage6_scoring.py`, `_stage7_email_qa.py`,
    `_stage8_attio.py`.

24. **`workspace_factory` test fixture/helper.** Reduces boilerplate
    in every test that needs a fresh workspace.

25. **Pure-function tests first, CLI tests second.** Once business
    logic is extracted (item 7), most tests stop needing
    subprocess-style full-pipeline runs.

26. **Golden fixtures for CSVs + Attio payloads.** This app is
    audit-heavy; golden outputs are high-value for catching silent
    drift.

## Operator Experience Refactors

27. **Unify status + doctor.** `core/operator_health.py` powers both
    the diagnostic surface (`status.py`) and the invariant validation
    (`doctor.py`). Currently overlapping logic in two places.

28. **Per-workspace runbook.** Based on `mode` + `required_systems`,
    print exact commands and blockers.

29. **Single pipeline command.** `scripts/run_pipeline.py --workspace
    ... --through 07`. Stops on first red stage, prints next repair
    action.

30. **Targeted check command.**
    `scripts/check_ready.py --for gmail`,
    `scripts/check_ready.py --for attio`.

## Sequencing

Suggested order once kicked off:
1. Items 1-3 (stage runner + result semantics + accounting helper) —
   the cheapest leverage; every subsequent refactor depends on them.
2. Items 23-25 (test refactor) before the big extractions — so the
   tests can exercise the new shapes as they land.
3. Items 7, 11-16 (business-logic extraction + stage splits) — these
   are the bulk of the work.
4. Items 4-6, 9-10 (CSV validators, fetch abstraction, migrations,
   mode policy) — supporting infrastructure.
5. Items 17-22 (data refactors) — the most invasive; should come last
   when the surrounding code is stable enough to absorb schema moves.
6. Items 27-30 (operator UX) — best done last, on top of the new
   modules.

## Pending before refactor pass kicks off

- Batch 41: outcome sync external_event_id wiring + cross-row dedup +
  missing-partner-join handling (#56, #57, #67)
- Batch 42: doctor source reachability + status "blocked because" +
  .env transparency (#72, #76, #80)
- Batch 43: test catch-up (#83, #84, #86, #87, #90)
