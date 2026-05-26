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
- Pitch-deck extraction is a setup assistant, not an automatic source of truth.

## Current Architecture

This is a Python, SQLite, CLI, and FastAPI application organized around one
workspace per raise/client.

- `scripts/` contains pipeline stages and operator commands.
- `core/` contains shared ingestion, scoring, approval, Attio, Gmail, migration,
  backup, locking, and operator-safety logic.
- `web/api.py` exposes the browser/API surface for onboarding, review,
  approvals, readiness checks, exports, Gmail OAuth, and pipeline actions.
- `clients/{workspace}/` contains one workspace's config, raw inputs, exports,
  local `.env`, and SQLite database.
- `clients/{workspace}/data/pipeline.db` is the workspace database.
- `ui_prototype/index.html` is still useful for design review, but the real web
  integration should use `web/api.py`, not direct SQLite writes.

The browser frontend should treat the API as the only mutating surface. It
should never edit SQLite directly.

## Quick Start

```bash
uv sync
cp .env.example .env
$EDITOR .env                         # add ANTHROPIC_API_KEY for live LLM use
uv run scripts/init_workspace.py my_raise
export INVESTOR_WORKSPACE=clients/my_raise
uv run scripts/status.py
```

Then configure the generated workspace:

- `clients/my_raise/config/company.yaml` - company, raise, ICP, scheduling link,
  mode, deliverability settings.
- `clients/my_raise/config/sources.yaml` - fund lists, fund URLs, RSS feeds, and
  live source settings.
- `clients/my_raise/config/axes.yaml` - scoring axes and weights.
- `clients/my_raise/config/attio.yaml` - optional Attio sync config.
- `clients/my_raise/prompts/examples/*.md` - voice examples for email drafting.

Every script accepts `--workspace clients/my_raise`; if `INVESTOR_WORKSPACE` is
set, the flag is optional.

## Web API

The FastAPI backend is intended for the external browser frontend.

Run locally:

```bash
API_KEY=dev-key \
INVESTOR_WORKSPACE=clients/test_workspace \
uv run --extra api uvicorn web.api:app --reload --port 8080
```

Useful routes:

| Route | Purpose |
|---|---|
| `GET /openapi.json` | Frontend type generation / route discovery |
| `GET /config` | Onboarding snapshot: mode, Gmail + Drive connection state, company-config status |
| `GET /config/company` | Read editable company setup profile |
| `PUT /config/company` | Save reviewed/edited company setup profile |
| `POST /config/company/extract-from-deck` | Extract a draft profile from a PDF/PPT/PPTX deck upload |
| `POST /config/mode` | Flip fixture/dry_run/production mode from the browser |
| `POST /pipeline/sources` | Upload an investor-sources CSV or XLSX (OpenVC export shape supported); writes under `data/raw/` and wires `sources.yaml` to it |
| `POST /pipeline/score` | Run Stage 6 from the browser |
| `POST /pipeline/generate` | Run Stage 7 from the browser |
| `GET /review/pending` | Drafts waiting for human review |
| `GET /drafts/approved` | Drafts that passed review and are eligible to send/export |
| `POST /drafts/{draft_id}/approve` | Approve a draft through the audited CLI path |
| `POST /drafts/{draft_id}/reject` | Reject a draft |
| `POST /partners/{partner_id}/email` | Set partner email |
| `GET /check_ready` | Review/send/Gmail/Attio readiness checks |
| `GET /runs` | Recent stage/operator run history |
| `GET /send_queue.csv` | Export approved outreach CSV |
| `POST /gmail/connect` | Start Google OAuth (Gmail + Drive in one consent step) |
| `GET /gmail/status` | Legacy single-scope Gmail connection check |
| `GET /google/status` | Per-scope status: `gmail_connected`, `drive_connected`, `google_connected` |

All protected routes require `Authorization: Bearer <API_KEY>`. The OAuth
callback is intentionally public because Google redirects the browser there with
a one-time state token.

## Deck-First Onboarding

The preferred setup flow starts with the founder's pitch deck.

1. User uploads a PDF/PPT/PPTX deck.
2. The API extracts a draft `CompanyProfile` from the deck.
3. The frontend populates the normal editable company setup form.
4. The frontend shows extracted-field confidence, evidence snippets, and
   missing/needs-review fields.
5. The user edits or confirms the profile.
6. Only then does the frontend call `PUT /config/company` to write
   `company.yaml`.
7. Onboarding continues normally into sources, Apollo/email import, scoring,
   draft generation, review, and Gmail/export readiness.

Deck extraction must not write `company.yaml` by itself. If extraction fails, or
if the deck is image-heavy and little text is available, the user can continue
with manual entry.

The extraction response is expected to include:

- `profile`: the draft `CompanyProfile`.
- `extracted_fields`: field, value, confidence, evidence, and source slide/page
  when available.
- `missing_required_fields`: required fields not found in the deck.
- `needs_review_fields`: low-confidence extracted fields.
- `warnings`: image-heavy deck, sparse text, unsupported content, etc.
- `source_filename` and `text_preview` for operator/debug visibility.

Fields the app should try to extract include company name, one-liner, website,
founder name/title/email, stage, sectors, business model, problem, solution,
differentiators, why now, traction, round amount, instrument, valuation, close
target, target investor criteria, and scheduling link if actually present.

Required setup fields before the user can finish onboarding:

- Company name.
- One-liner.
- Founder name.
- Founder email.
- Stage/round.
- Problem.
- Solution.
- Traction.
- Target sectors.
- Scheduling link.

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

1. Create or select a workspace.
2. Upload the pitch deck and review/edit extracted company setup fields.
3. Configure fund sources and scoring axes.
4. Run Stages 1-7 to discover funds, enrich partners, mine evidence, score, and
   generate drafts.
5. Export partners for Apollo.
6. Upload/import Apollo emails.
7. Review draft emails.
8. Approve or reject drafts.
9. Run `check_ready.py` for the intended next action.
10. Create Gmail drafts or export a send queue.
11. Optionally sync approved context to Attio.
12. Record outcomes and use them for future calibration.
13. For substantive replies or booked meetings, generate an investor dossier /
    prep brief.

A typical first CLI run after setup:

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

## Investor-Sources Upload

The wizard's Step 3 lets the founder upload an investor list as the starting
point of the fund universe. Both shapes are accepted:

- `.csv` with `name`/`domain` columns, or any common third-party shape
  (`Investor name`/`Website`, `Firm`/`URL`, `Fund name`/`Homepage`, etc.).
  Headers are aliased case-insensitively; OpenVC's standard CSV export is the
  primary target.
- `.xlsx` with the same column shape (typical OpenVC dashboard export).
  Converted to CSV in-memory via openpyxl; the on-disk persistence is always
  CSV so Stage 1's parser only needs one code path.

Behavior:

1. Frontend uploads the file via `POST /pipeline/sources` (multipart, field
   `file`).
2. Backend validates the file extension and content (rejects empty,
   header-only, and oversized uploads), normalizes headers to lowercase, and
   writes `clients/{workspace}/data/raw/<sanitized>.csv`.
3. The new file is prepended to `sources.yaml`'s `public_lists` so the next
   Stage 1 run picks it up automatically.
4. The endpoint returns `{ok, row_count, stdout}` so the wizard can confirm
   "Loaded N investors".

The upload is idempotent on filename: re-uploading the same name overwrites
the CSV but does not duplicate the `sources.yaml` entry. Path-traversal
characters in the filename are stripped so uploads always land inside
`data/raw/`.

Stage 1 then reads the file with `name` + `domain` extracted via aliases (URL
hosts are stripped so `https://www.foo.org/...` lands as `foo.org`). Rows
missing either field are silently skipped.

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

## Google: Gmail Drafts, Send Queue, and Drive Sync

The same OAuth flow grants two Google scopes in one consent step so the
wizard's "Connect Google" button covers both surfaces:

- `gmail.compose` for creating draft messages (cannot send).
- `drive.file` (narrow scope: only files this app creates) for pushing
  meeting-prep dossiers into the operator's Drive.

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

`GET /google/status` returns per-scope booleans so the wizard can distinguish
"Gmail granted but Drive needs re-consent" (typical for tokens minted before
the Drive scope was added) from "fully connected".

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

Attio outcome sync exists as a background/import path. When a substantive reply
or meeting signal arrives, the local workspace should hydrate relationship /
outcome state and create the appropriate follow-up work item, such as an
investor dossier task.

## Outcomes, Dossiers, and Learning

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

For substantive replies, active conversations, or booked meetings, use the
meeting-prep path rather than stuffing extra detail into cold emails. The prep
artifact should synthesize verified signals, company setup, Attio/outcome
context, and optional live research into an investor dossier that helps the
founder run a better meeting.

The intended dossier structure is:

- Profile summary.
- How this investor thinks.
- Firm snapshot.
- Fit assessment.
- Pitch framing.
- Topics to handle carefully.
- Anticipated objections/questions.
- Closing posture.
- Sources.

Cold email should stay short. Dossier depth belongs after the investor replies
or a meeting is booked.

When a substantive outcome lands (`replied` with a non-pass reply type, or
`meeting_booked`), `persist_outcome_event` automatically creates a review item
of kind `investor_dossier_needed`. Run

```bash
uv run scripts/prep_brief.py --dossier --pending-only
```

to build dossiers for every open task in one pass and mark them resolved. For a
single partner, `--partner-id <id> --dossier` works too. The dossier respects
an eligibility gate (only post-reply partners qualify) unless
`--force-refresh` is passed.

If the workspace has Drive connected, the dossier markdown is also auto-pushed
into an `investor_outreach_briefs` folder in the operator's Drive as a native
Google Doc. Filenames are stable across re-runs against the same verified
signal set (`{partner_id}__YYYY-MM-DD__investor_dossier__{signal_hash[:8]}`),
so unchanged evidence does not produce duplicate Drive uploads. `--no-drive-push`
opts out per-run.

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
| `scripts/prep_brief.py` | Generate a partner meeting/reply brief or dossier |
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
and sync behavior, Gmail readiness gates, immutable history, API onboarding, and
dry-run E2E paths.

## Configuration Reference

Minimum live workspace needs:

- `.env` with `ANTHROPIC_API_KEY` for live LLM stages.
- `company.yaml` with company, raise, scheduling link, founder/sender context,
  mode, and deliverability settings.
- `sources.yaml` with fund sources and funding announcement feeds.
- `axes.yaml` with scoring weights.
- Voice examples in `prompts/examples/` for better email generation.

Optional:

- Pitch deck upload through the web onboarding flow to prefill company setup.
- `attio.yaml` plus `ATTIO_API_KEY` for Attio sync.
- Gmail OAuth credentials/token for Gmail draft creation.
- Apollo CSV exports/imports for partner emails.

## What The App Does Not Do

- It does not automatically send emails.
- It does not run a warm-intro workflow.
- It does not guarantee partner emails without Apollo/manual import.
- It does not use Attio as the approval source of truth.
- It does not require Attio or Gmail unless you choose those paths.
- It does not treat deck extraction as confirmed company truth until the user
  reviews and saves the profile.
- It should not run investor dossiers for every cold prospect; dossier depth is
  for replies, active conversations, or booked meetings.

## Launch Checklist

Before using this on a real raise:

1. Confirm all open safety PRs are merged or intentionally closed.
2. Run `uv run pytest tests/ -q` on current `main`.
3. Create a fresh non-fixture workspace.
4. Upload the pitch deck, review extracted setup fields, and save the confirmed
   company profile.
5. Set `mode: dry_run` and run Stages 1-7.
6. Export partners for Apollo and import emails.
7. Review and approve a small number of drafts.
8. Run `check_ready.py --for send`.
9. Connect Gmail and run `check_ready.py --for gmail`.
10. Create Gmail drafts for a tiny batch and inspect them manually.
11. Switch to `mode: production` only after the dry run is clean.

## Known Limitations

- Deck extraction depends on readable text. Image-heavy decks can require manual
  setup edits.
- Live fund websites vary widely. Stage 2 has homepage link discovery and fixed
  fallback paths, but some sites will still require manual source correction.
- Partner employment status is only as good as the available fund/team evidence
  and manual corrections.
- Partner emails require Apollo/manual import.
- Attio matching can still need operator review when CRM records are ambiguous.
- Gmail OAuth must be validated in the operator's real environment.
- Investor dossiers are only as good as verified local evidence plus any
  explicitly enabled and cited research.
