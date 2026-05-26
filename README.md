# Investor Outreach Pipeline

Investor Outreach Pipeline is a workspace-based tool for finding relevant VC
funds and partners, enriching them with evidence, scoring who is worth
prioritizing, drafting tailored cold outreach, requiring human approval, and
then exporting or syncing only approved outreach.

The product stance is intentionally conservative:

- No email is sent automatically.
- Every outbound email must be approved by a human first.
- Warm-intro routing is not part of the product workflow.
- Apollo is handled through CSV export/import, not a direct Apollo API.
- Attio is CRM/context sync. Local approval state remains the source of truth.

## Current Architecture

This is currently a Python CLI and SQLite application with a static UI
prototype. It is not yet a conventional hosted web app with a public backend
API.

- `scripts/` contains pipeline stages and operator commands.
- `core/` contains shared ingestion, scoring, approval, Attio, Gmail, migration,
  backup, locking, and operator-safety logic.
- `clients/{workspace}/` contains one workspace's config, raw inputs, exports,
  local `.env`, and SQLite database.
- `clients/{workspace}/data/pipeline.db` is the workspace database.
- `ui_prototype/index.html` is a mock-data operator console. It is useful for UI
  design and workflow review, but it does not yet talk to SQLite or external
  APIs.

Longer term, the frontend should talk to a small local Python API that wraps the
same core modules and scripts. It should not directly mutate SQLite.

## Quick Start

```bash
uv sync
cp .env.example .env
$EDITOR .env                         # add ANTHROPIC_API_KEY for live LLM use
uv run scripts/init_workspace.py my_raise
export INVESTOR_WORKSPACE=clients/my_raise
uv run scripts/status.py
```

Then edit the generated workspace files:

- `clients/my_raise/config/company.yaml` - company, raise, ICP, scheduling link,
  mode, deliverability settings.
- `clients/my_raise/config/sources.yaml` - fund lists, fund URLs, RSS feeds, and
  live source settings.
- `clients/my_raise/config/axes.yaml` - scoring axes and weights.
- `clients/my_raise/config/attio.yaml` - optional Attio sync config.
- `clients/my_raise/prompts/examples/*.md` - voice examples for email drafting.

Every script accepts `--workspace clients/my_raise`; if `INVESTOR_WORKSPACE` is
set, the flag is optional.

## Workspace Modes

`company.yaml` controls mode.

| Mode | Purpose | External writes |
|---|---|---|
| `fixture` | Test workspace and CI fixtures | Refused unless explicitly overridden |
| `dry_run` | Real-ish setup without CRM/Gmail mutation | Refused |
| `production` | Real operator workflow | Allowed when checks pass |

Production mode is the only mode intended for real Gmail draft creation or
Attio writes. `check_ready.py` and the stage runners surface blockers before an
unsafe action runs.

## Happy Path

The normal workflow is:

1. Scaffold and configure a workspace.
2. Run Stages 1-7 to discover funds, enrich partners, mine evidence, score, and
   generate drafts.
3. Export partners for Apollo.
4. Upload/import Apollo emails.
5. Review draft emails.
6. Approve or reject drafts.
7. Run `check_ready.py` for the intended next action.
8. Create Gmail drafts or export a send queue.
9. Optionally sync approved context to Attio.
10. Record outcomes and use them for future calibration.

A typical first run:

```bash
uv run scripts/01_aggregate_sources.py
uv run scripts/02_enrich_funds.py
uv run scripts/03_mine_activity.py
uv run scripts/04_mine_partner_signals.py
uv run scripts/05_verify_and_quality.py
uv run scripts/06_score_candidates.py
uv run scripts/07_generate_emails.py --top 25
uv run scripts/status.py
```

## Pipeline Stages

| Stage | Script | What it does | Main inputs |
|---|---|---|---|
| 0 | `00_verify_attio_schema.py` | Optional Attio schema verification | `attio.yaml`, `ATTIO_API_KEY` |
| 1 | `01_aggregate_sources.py` | Aggregates fund targets | CSV/markdown paths and URLs from `sources.yaml` |
| 2 | `02_enrich_funds.py` | Fetches fund sites, discovers likely team/portfolio/about/news pages, extracts partners and fund facts | Fund URLs/domains, fixed paths, homepage link discovery |
| 3 | `03_mine_activity.py` | Mines funding announcements and attributes deals to funds/partners | RSS feeds or fixtures |
| 4 | `04_mine_partner_signals.py` | Mines partner evidence and quotes | `partner_content_urls.csv`, Stage 2 page fallback, or fixtures |
| 5 | `05_verify_and_quality.py` | Verifies and quality-scores evidence | Source snapshots and signal URLs |
| 6 | `06_score_candidates.py` | Scores partner priority and recommendation state | Verified signals, deal attributions, fund facts, company config |
| 7 | `07_generate_emails.py` | Generates draft emails and review artifacts | Recommended partners, prompts, voice examples |
| 8 | `08_sync_to_attio.py` | Optional Attio company/person sync | Approved drafts and Attio config |

Stages that need live LLM behavior refuse loudly when required API keys are
missing instead of silently producing empty state.

## Human Approval Model

Stage 7 generates drafts for human review. It does not make anything sendable by
itself.

Important states:

- `needs_review` - generated and waiting for human review.
- `qa_failed` - generated but failed quality checks.
- `approved_to_send` - human approved and eligible for send/export/sync gates.
- `rejected` - human rejected.
- `stale_after_approval` - was approved, but later data changed and invalidated
  the approval.

Approvals are append-only through `draft_approvals`. `email_drafts` carries the
current pointer/status for fast operator queries. Mutating partner or fund state
can stale previously approved drafts.

Common invalidation triggers include:

- Partner email changed.
- Partner email cleared or marked invalid/risky.
- Partner marked do-not-contact.
- Partner marked as having left the fund.
- Fund marked inactive.
- Relationship/outcome suppression changes.
- Stage 7 generates materially different content for a previously approved
  partner.

## Apollo Email Workflow

Apollo is external enrichment by CSV.

```bash
uv run scripts/export_partners_for_apollo.py --out clients/my_raise/exports/apollo_upload.csv
# Upload to Apollo, enrich emails, download CSV.
uv run scripts/import_partner_emails_apollo.py --from-csv path/to/apollo_results.csv
uv run scripts/status.py
```

The importer validates partner IDs, email format, duplicates, conflicts, and
unknown rows. Conflicting email updates require an explicit overwrite. Email
changes stale live approvals so old approved drafts cannot go out to a changed
recipient unnoticed.

## Reviewing Drafts

List pending drafts:

```bash
uv run scripts/list_pending_review.py
```

Approve or reject:

```bash
uv run scripts/approve_draft.py --draft-id 123 --notes "Looks good"
uv run scripts/reject_draft.py --draft-id 123 --reason "Wrong hook"
```

The approval gate re-checks live partner and workspace state at approval time.
It blocks missing emails, do-not-contact partners, stale/superseded drafts,
invalid/risky email status, QA failures, relationship suppression, and other
hard blockers. Overrides require an explicit reason and are recorded.

## Readiness Checks

Use `check_ready.py` before acting.

```bash
uv run scripts/check_ready.py --for review
uv run scripts/check_ready.py --for send
uv run scripts/check_ready.py --for gmail
uv run scripts/check_ready.py --for attio
```

| Phase | Checks |
|---|---|
| `review` | Workspace and pipeline state, pending/approved draft availability |
| `send` | Approved drafts, emails, duplicate recipients, approval gate, daily cap |
| `gmail` | Send checks plus Gmail OAuth and scheduling-link readiness |
| `attio` | Send checks plus Attio config/API-key readiness |

`--for gmail` is strict: unlinked Gmail or a missing scheduling link is blocked.

## Gmail Drafts and Send Queue

Gmail integration creates drafts only. It does not send mail.

```bash
uv run scripts/connect_gmail.py
uv run scripts/check_ready.py --for gmail
uv run scripts/create_gmail_drafts.py --top 25
```

For CSV-based sending or manual review:

```bash
uv run scripts/export_send_queue.py --out clients/my_raise/exports/send_queue.csv
```

Only `approved_to_send` drafts should leave the system. Downstream commands
re-check approval state as defense in depth.

## Attio Sync

Attio is optional.

```bash
uv run scripts/00_verify_attio_schema.py
uv run scripts/check_ready.py --for attio
uv run scripts/08_sync_to_attio.py --top 25
```

Stage 8 syncs companies and people, preserves Attio-owned fields when outreach
has already started, and logs every attempted mutation in `attio_sync_log`.
Draft bodies and subject lines are only sent to Attio after human approval.

Attio outcome sync exists as a background/import path, but local outcome state is
still explicit and auditable.

## Outcomes and Learning

Record outcomes manually or by CSV:

```bash
uv run scripts/record_outcome.py --partner-id p_example --status replied --reply-type interested
uv run scripts/record_outcome.py --from-csv clients/my_raise/data/raw/outcomes.csv
```

Reply classification is assisted but not automatic:

```bash
uv run scripts/classify_reply.py --partner-id p_example --file reply.eml
```

The classifier asks for operator confirmation before recording. Outcomes hydrate
relationship/suppression fields and can stale approvals when outreach should no
longer proceed.

Calibration tools help keep scoring honest:

```bash
uv run scripts/calibration.py --start
uv run scripts/calibration.py --status
uv run scripts/calibration.py --complete --outcome green --reason "Strong replies"
```

## Operator Commands

Common no-SQL commands:

| Command | Purpose |
|---|---|
| `scripts/init_workspace.py NAME` | Guided workspace setup/scaffold |
| `scripts/status.py` | Operator dashboard in the terminal |
| `scripts/check_ready.py` | Safety gate for review/send/Gmail/Attio |
| `scripts/export_partners_for_apollo.py` | Export partner rows for Apollo enrichment |
| `scripts/import_partner_emails_apollo.py` | Import Apollo-enriched emails |
| `scripts/list_pending_review.py` | Show drafts waiting for review |
| `scripts/approve_draft.py` | Approve a draft for send/export/sync |
| `scripts/reject_draft.py` | Reject a draft |
| `scripts/set_partner_email.py` | Set or bulk-import partner emails |
| `scripts/set_relationship.py` | Manually update relationship/outcome state |
| `scripts/set_do_not_contact.py` | Mark a partner do-not-contact |
| `scripts/set_fund_inactive.py` | Mark a fund inactive |
| `scripts/set_employment_status.py` | Update partner employment status |
| `scripts/manual_override.py` | Manage score/recommendation overrides |
| `scripts/promote_provisional.py` | Promote or merge provisional funds from Stage 3 |
| `scripts/bulk_reattribute.py` | Re-attribute deal rows after fund corrections |
| `scripts/review_attribution.py` | Resolve ambiguous attribution review items |
| `scripts/prep_brief.py` | Generate a partner meeting/reply brief |
| `scripts/connect_gmail.py` | Link Gmail OAuth for a workspace |
| `scripts/create_gmail_drafts.py` | Create Gmail drafts for approved outreach |
| `scripts/export_send_queue.py` | Export approved outreach to CSV |

Mutating operator commands run under workspace locks and backups where
appropriate, so concurrent stage/operator writes do not race the SQLite DB.

## Data and Safety Infrastructure

Important safety systems:

- Workspace run lock prevents concurrent stages from racing the same SQLite DB.
- Pre-stage and pre-operator backups protect destructive operations.
- Versioned migrations track schema changes.
- `runs` and `run_errors` record stage/operator outcomes.
- `source_snapshots` and source IDs preserve provenance.
- Review queues capture ambiguous attribution and other human decisions.
- Immutable draft history preserves prior generated artifacts instead of hard
  deleting them.
- Pipeline batch IDs connect sources, signals, scores, drafts, approvals, syncs,
  and outcomes.

## UI Prototype

Open:

```bash
open ui_prototype/index.html
```

The prototype covers setup, runbook/status, partner review, Apollo email import,
draft approval, partner detail, and readiness workflows. It is currently static
mock data. A real frontend should use a local API wrapper around existing core
logic rather than direct DB writes.

## Test Workspace

Fixture run:

```bash
uv run scripts/01_aggregate_sources.py    --workspace clients/test_workspace
uv run scripts/02_enrich_funds.py         --workspace clients/test_workspace --fixtures
uv run scripts/03_mine_activity.py        --workspace clients/test_workspace --fixtures
uv run scripts/04_mine_partner_signals.py --workspace clients/test_workspace --fixtures
uv run scripts/05_verify_and_quality.py   --workspace clients/test_workspace
uv run scripts/06_score_candidates.py     --workspace clients/test_workspace
uv run scripts/07_generate_emails.py      --workspace clients/test_workspace --top 5
uv run scripts/check_ready.py             --workspace clients/test_workspace --for review
```

CI runs without live API keys by using fixtures and stubbed LLM paths.

## Tests

```bash
uv run pytest tests/ -q
```

The suite covers stage behavior, operator CLIs, migrations, run locking,
approval gates, stale approval invalidation, Apollo import/export, Attio payload
and sync behavior, Gmail readiness gates, immutable history, and dry-run E2E
paths.

## Configuration Reference

Minimum live workspace needs:

- `.env` with `ANTHROPIC_API_KEY` for live LLM stages.
- `company.yaml` with company, raise, scheduling link, founder/sender context,
  mode, and deliverability settings.
- `sources.yaml` with fund sources and funding announcement feeds.
- `axes.yaml` with scoring weights.
- Voice examples in `prompts/examples/` for better email generation.

Optional:

- `attio.yaml` plus `ATTIO_API_KEY` for Attio sync.
- Gmail OAuth credentials/token for Gmail draft creation.
- Apollo CSV exports/imports for partner emails.

## What The App Does Not Do

- It does not automatically send emails.
- It does not run a warm-intro workflow.
- It does not guarantee partner emails without Apollo/manual import.
- It does not use Attio as the approval source of truth.
- It does not require Attio or Gmail unless you choose those paths.
- It does not live-research during meeting brief generation; brief output should
  synthesize existing verified signals.

## Planned Extension: Meeting Prep

The next planned extension is meeting prep after a substantive reply or booked
meeting. This should extend `scripts/prep_brief.py`; it should not add a new
pipeline stage and should not affect cold-outreach email generation.

Goal: for partners with `outreach_status IN ('replied', 'meeting_booked')`,
produce two additional artifacts:

- Objection map: 5-7 likely objections grounded in verified quality>=2 signals,
  fund kill signals, deal attribution patterns, and clearly labeled sector norms.
- Framing brief: how to tell the story in the actual meeting, including what to
  lead with, amplify, address unprompted, avoid leading with, and ask them.

Proposed files:

```text
core/meeting_prep/
  __init__.py
  objection_map.py
  framing_brief.py
schemas/
  objection_map.py
  framing_brief.py
prompts/
  objection_map.txt
  framing_brief.txt
scripts/prep_brief.py
core/db.py                # meeting_prep_artifacts cache table
```

Hard rules for that build:

- Every non-generic objection must cite at least one verified quality>=2
  `signal_id`.
- Partners with fewer than two quality>=2 signals should return
  `insufficient_evidence=True` and a short note rather than fabricated analysis.
- Cache by `(partner_id, signal_set_hash)` so reruns are free unless evidence
  changed.
- Do not run on every partner. Default only for replied/booked-meeting partners;
  require explicit opt-in otherwise.
- Do not perform live web research at brief time.
- Do not add portfolio-overlap analysis until Stage 2 persists structured
  `portfolio_companies`.

Proposed CLI:

```bash
uv run scripts/prep_brief.py \
  --partner-id p_acme_partner_jane \
  --include-objections --include-framing \
  --out clients/my_raise/exports/briefs/jane.md
```

The output should keep the existing prep brief sections at the top and append:

- `Objections to prepare for`
- `How to tell your story today`

This is the natural first place to consume richer `company.yaml` fields like
`problem`, `solution`, `differentiators`, `why_now`, `desired_traits`,
`excluded_sectors`, and `do_not_contact`, because meeting prep has enough
context for that nuance. Cold email should stay short.

## Launch Checklist

Before using this on a real raise:

1. Confirm all open safety PRs are merged or intentionally closed.
2. Run `uv run pytest tests/ -q` on current `main`.
3. Create a fresh non-fixture workspace.
4. Set `mode: dry_run` and run Stages 1-7.
5. Export partners for Apollo and import emails.
6. Review and approve a small number of drafts.
7. Run `check_ready.py --for send`.
8. Connect Gmail and run `check_ready.py --for gmail`.
9. Create Gmail drafts for a tiny batch and inspect them manually.
10. Switch to `mode: production` only after the dry run is clean.

## Known Limitations

- Live fund websites vary widely. Stage 2 has homepage link discovery and fixed
  fallback paths, but some sites will still require manual source correction.
- Partner employment status is only as good as the available fund/team evidence
  and manual corrections.
- Partner emails require Apollo/manual import.
- Attio matching can still need operator review when CRM records are ambiguous.
- Gmail OAuth must be validated in the operator's real environment.
- Meeting-prep objection/framing artifacts are planned, not yet current runtime
  behavior.
