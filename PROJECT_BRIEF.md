# Investor Outreach Pipeline: Build Spec (v2)

A Claude Code-buildable pipeline that builds a verified, scored cold outreach target list of VC partners, drafts personalized emails per partner designed to convert to a pitch meeting, and produces a reviewable CSV plus optional Attio sync. Designed to be reusable across founders, companies, raises, and clients via a workspace-based config layer.

---

## How to use this spec

1. Save this file as `PROJECT_BRIEF.md` in an empty directory.
2. Open Claude Code in that directory.
3. Paste this into the first message: "Read PROJECT_BRIEF.md in full before starting. Then build the system one Build Session at a time. Follow the Agent Build Rules strictly. Use the Execution Gates to decide when to scale data volume. At the end of every Build Session, stop and produce a Session Check-In before continuing. Ask me clarifying questions before writing code."
4. Before running anything end to end, populate `clients/{workspace}/config/company.yaml`, `clients/{workspace}/config/axes.yaml`, and (only when ready to use Attio) `clients/{workspace}/config/attio.yaml`. The CSV path runs without Attio configured.

---

## What the system does

This is raise-time outreach infrastructure. It exists because the user is actively raising a round and needs to deliver pitch meetings to fit, lead-capable, currently-deploying investors. The architecture is designed so the same codebase serves this raise, the user's next raise, and other founder clients without code changes — only new workspace directories.

Inputs (per workspace): config files describing the company, the active raise context, the thesis axes, the target investor profile, round-fit criteria, founder voice, hand-written email examples, and optionally an Attio workspace.

Outputs (per workspace):

- A verified universe of up to ~200 high-confidence VC partner candidates stored in workspace SQLite
- A CSV review queue at `clients/{workspace}/exports/review_queue.csv` containing the top 25 partners with: composite fit score, deterministic round fit score, lead likelihood score, cold reachability score, send-now priority, top verified signals with source URLs, two email draft variants asking for a pitch meeting, a pre-written deck-request deflection reply, a follow-up draft, and a recommended-to-send flag
- Optional sync of the same data into Attio
- Outcome tracking via Attio (or manual CSV import) for monthly learning patterns

The single most important output is a partner-specific email that converts to a booked pitch meeting for the current raise. Every prior stage exists to produce that email at quality. Success is measured in pitch meetings booked, not emails sent, not responses received, not "send me the deck" replies.

### Goals

1. Raise-time outreach for the founder running this workspace.
2. Reusable across raises (the same founder's later rounds) by spinning up a new workspace directory.
3. Reusable across clients (other founders) by spinning up a new workspace directory. Code remains tenant-agnostic.
4. CSV-first: the system must produce reviewable output without requiring Attio.

### Non-goals

- Warm path discovery (handled outside this system through user network)
- Sending emails (drafts go to CSV and optional Attio; user sends via their normal workflow)
- Replacing Attio (CRM, pipeline tracking, notes, tasks all stay in Attio when used)
- Matching paid databases like Crunchbase or Harmonic on coverage
- Networking, relationship building, thesis discussions, or any meeting type that is not a pitch meeting for the active round
- Multi-tenant SaaS (no auth, no UI, no account system; tenancy is filesystem-level only)

---

## Tenant-Agnostic Design

This system must be reusable across founders, companies, raises, and clients.

All per-instance state lives in `clients/{workspace}/config/`, `clients/{workspace}/data/`, `clients/{workspace}/exports/`, and `clients/{workspace}/prompts/examples/`.

Code under `core/` and `scripts/` must not contain client-specific assumptions, company names, investor axes, founder voice, Attio workspace IDs, raise context, or outcome history. A new client or raise should require a new configured workspace directory, not code changes.

Every script accepts a `--workspace clients/{name}` argument:

```bash
uv run scripts/01_aggregate_sources.py --workspace clients/oko_seed
uv run scripts/07_generate_emails.py --workspace clients/oko_seed
```

The first thing built in Session 1 is the workspace-aware config loader. Every subsequent script imports from it. If it is stubbed or skipped, the tenant-agnostic constraint silently rots within the first session.

---

## Founder Time Guardrail

This system must not consume unlimited founder attention during an active raise.

**Build target:**
- 8 hours to first usable vertical slice (CSV output for one partner end to end)
- 12 hours max before cutting scope or stopping to reassess
- If the v1 researcher + drafting + CSV workflow is not usable after the agreed build window, stop building and switch to manual outreach for this raise

**Operating target:**
- 25 reviewed records per batch
- 4 hours/week max founder review time during a raise
- This system runs in parallel with warm intro work. It is not a substitute for warm intros, product milestones, pitch prep, or investor meetings.

**Weekly allocation rule during an active raise:**
- 2-3 hours on this system
- Rest of fundraising time on warm intros, meetings, customer/proof work, follow-up

---

## Secrets and Environment Boundaries

Shared service keys live in the repo-root `.env`. Examples:
- `ANTHROPIC_API_KEY`
- `LISTENNOTES_API_KEY`
- any other shared enrichment/search APIs

Workspace-specific keys live in `clients/{workspace}/.env` and override root-level values. Examples:
- `ATTIO_API_KEY`
- `ATTIO_WORKSPACE_ID`
- workspace-specific export paths or CRM settings

The config loader resolves environment variables in this order:
1. Repo-root `.env`
2. `clients/{workspace}/.env`
3. Process environment variables (override both)

External API rate limiters for shared providers, especially Anthropic, must be process-wide rather than workspace-local. If multiple workspaces run concurrently, they share the same provider-level limiter to avoid 429s. Implement as a module-level singleton in `core/llm/limiter.py`.

---

## Database Scale vs Send Scale

The system may research and score 200 partners, but it only prepares 25 reviewed sends at a time.

- Research scale: up to 200 partners per workspace
- Review scale: 25 partners per batch
- Send scale: 10-25 emails per week, user-controlled

The system's job is to maintain a reusable investor universe and produce high-quality reviewed batches, not to maximize weekly send volume. The database can hold the full 200 scored partners; only 25 at a time are marked `ready_to_send`.

---

## Architecture

Eight pipeline stages plus two background jobs. The first vertical slice (Session 1) is a thin end-to-end pass; subsequent sessions thicken each stage.

1. Aggregate fund universe from free public sources
2. Enrich each fund by scraping their website
3. Mine 12 months of funding announcements for activity attribution
4. Mine partner-level public signals (podcasts, writing, social), extract thesis signals and cold reachability evidence
5. Verification gauntlet (URL resolution + quote substring match + snapshot fallback) AND signal quality scoring (0-3) in the same script
6. Score each partner: 4-axis composite (LLM, thesis/personality fit only), deterministic round_fit (observable facts), lead_likelihood (mostly deterministic), send_now_priority (weighted composite)
7. Generate two draft emails per top-25-scored partner, optimized for meeting conversion. Write CSV review queue.
8. Optional sync to Attio

Background jobs:

- `jobs/attio_outcome_sync.py`: pulls response and meeting status from Attio back into workspace SQLite (when Attio is configured)
- `jobs/monthly_learning_report.py`: produces suggested axis-weight diffs and operational pattern reports for human approval (never auto-applies)

Local SQLite (per workspace) is the working store. CSV is the primary deliverable. Attio is the system of record only when configured.

---

## Tech stack (do not ask, just use)

- Language: Python 3.11+
- Package manager: `uv`
- HTTP: `httpx` with async
- HTML parsing: `selectolax`
- LLM: `anthropic` SDK. Claude Sonnet for batch work, Claude Opus for email generation
- Attio: `httpx` against the v2 REST API (no official Python client)
- Database: SQLite via `sqlalchemy`
- Config: YAML via `pyyaml`
- Schema validation: `pydantic` for all LLM output parsing
- Rate limiting: `aiolimiter` (process-wide for shared providers)
- Retry: `tenacity` for all external calls
- Similarity: `rapidfuzz`
- Job orchestration: plain Python scripts run in order

---

## Project structure

```
investor-outreach/
├── PROJECT_BRIEF.md
├── README.md
├── pyproject.toml
├── .env                                # shared keys (Anthropic, ListenNotes, etc.)
├── .env.example
├── core/                               # tenant-agnostic code
│   ├── __init__.py
│   ├── config_loader.py                # workspace-aware config; built FIRST in Session 1
│   ├── db.py                           # SQLAlchemy models, per-workspace db init
│   ├── http_client.py                  # async httpx, retries, timeouts, per-domain rate limit
│   ├── llm/
│   │   ├── client.py                   # JSON-validated LLM calls, retry-on-bad-json
│   │   └── limiter.py                  # process-wide singleton rate limiter
│   ├── attio_client.py                 # thin v2 API wrapper
│   ├── runs.py                         # run logging
│   ├── verification.py                 # URL resolution, substring matching, snapshot fallback
│   ├── signal_quality.py               # 0-3 scoring against shared calibration set
│   ├── round_fit.py                    # deterministic round_fit calculation
│   ├── lead_likelihood.py              # mostly deterministic lead_likelihood calculation
│   ├── similarity.py                   # rapidfuzz wrappers, normalized 0.0-1.0
│   ├── csv_export.py                   # writes review_queue.csv
│   └── calibration/
│       └── signal_quality_examples.json  # shared, company-agnostic 0-3 examples
├── scripts/
│   ├── 00_verify_attio_schema.py
│   ├── 01_aggregate_sources.py
│   ├── 02_enrich_funds.py
│   ├── 03_mine_activity.py
│   ├── 04_mine_partner_signals.py
│   ├── 05_verify_and_quality.py
│   ├── 06_score_candidates.py
│   ├── 07_generate_emails.py
│   └── 08_sync_to_attio.py
├── schemas/                            # pydantic schemas for LLM output validation
│   ├── fund_enrichment.py
│   ├── partner_signals.py
│   ├── candidate_score.py
│   ├── deal_attribution.py
│   ├── signal_quality.py
│   └── email_generation.py
├── prompts/                            # shared, parameterized prompts
│   ├── enrich_fund.txt
│   ├── extract_partner_signals.txt
│   ├── score_candidate.txt
│   ├── attribute_deal.txt
│   ├── signal_quality.txt
│   └── generate_email.txt
├── jobs/
│   ├── attio_outcome_sync.py
│   ├── monthly_learning_report.py
│   └── apply_axis_suggestion.py
└── clients/                            # per-workspace state
    └── {workspace}/
        ├── .env                        # workspace-specific keys (Attio, etc.)
        ├── config/
        │   ├── company.yaml
        │   ├── axes.yaml
        │   ├── sources.yaml
        │   └── attio.yaml              # optional; only when syncing to Attio
        ├── data/
        │   ├── raw/
        │   ├── enriched/
        │   ├── activity/
        │   ├── signals/
        │   ├── fixtures/               # tiny test datasets per stage
        │   └── pipeline.db
        ├── exports/
        │   └── review_queue.csv
        └── prompts/
            └── examples/               # founder-specific hand-written examples
                ├── signal_led.md
                ├── portfolio_led.md
                ├── market_shift_led.md
                ├── round_pattern_led.md
                ├── contrarian_thesis_led.md
                ├── traction_led.md
                ├── follow_up.md
                └── deck_request_response.md
```

---

## Agent Build Rules

These are non-negotiable.

1. **Build the workspace-aware config loader FIRST.** Before any stage script, the loader in `core/config_loader.py` must work and be importable. Every subsequent script reads from it. No hardcoded paths, no client-specific defaults.
2. **Build one script at a time after the vertical slice.** Sessions 2-9 thicken each stage in order. Do not write Stage 5 before Stage 4 is verified working.
3. **Session 1 produces a thin vertical slice end to end.** Stub where needed. The goal is to prove the data shape and CSV output before thickening any stage.
4. **Create a tiny fixture dataset for each stage before scaling.** For each script, the first execution runs against a fixture of 5 funds or 10 partners stored in `clients/{workspace}/data/fixtures/`. The script must produce correct output on the fixture before any real data is touched.
5. **Never start broad scraping until the pipeline works on the fixture end to end.** Stages 1 through 7 (CSV path) must execute cleanly on the fixture before any real-world batch run.
6. **Every LLM output must be parsed as JSON and validated against a pydantic schema.** No free-text parsing. No regex on LLM output. Malformed JSON triggers up to 3 retries with stricter prompts, then logs and skips.
7. **Every external call must have retry, timeout, and rate-limit handling.** `tenacity` for retries (exponential backoff, max 3 attempts), `httpx` timeouts (30 seconds default), `aiolimiter` for per-domain and process-wide per-API rate limits. No bare `requests.get`.
8. **Every stage must be re-runnable without duplicating records.** Use idempotent upserts keyed on canonical IDs (domain for funds, deterministic `partner_id` slug for partners). Re-running Stage 2 on the same fund must update its record, not create a duplicate.
9. **Manual overrides must be preserved.** If a user has flipped `manual_score_override=TRUE` or `manual_recommended_override=TRUE`, the pipeline preserves the affected fields. Force-refresh requires an explicit `--force-rescore --reason "..."` flag with logged justification.
10. **Every record synced to Attio must preserve user-edited fields if outreach has already started.** If `outreach_status` is `sent` or beyond, do not overwrite `outreach_email_draft`, `email_subject_line`, or any user-edited fields. Update only scoring fields and meta.
11. **Every quoted signal must include source URL, quote text, date if known, and verification status.** Signals missing any of these are invalid and excluded from scoring.
12. **If verification fails, the signal cannot be used in scoring or email generation.** A signal whose URL does not resolve, or whose quoted text does not substring-match the fetched page content (or its snapshot), is marked `verified=False` and excluded downstream. No exceptions.
13. **Signal quality gate applies after verification.** Only `signal_quality_score >= 2` may support scoring. Only `signal_quality_score >= 3` may be used as a primary email opener.
14. **All errors are logged to the workspace's `pipeline.db` in a runs table.** Stage, record ID, error message, timestamp. Silent failures are forbidden. Every run produces a summary at the end: records processed, succeeded, failed, skipped.
15. **The agent must stop at every Build Session boundary AND at every Execution Gate.** Wait for explicit user approval before proceeding.
16. **The system never sends emails, never bulk-exports a send queue beyond 25 records per day, and never marks more than 25 records per day as `ready_to_send` without explicit human approval logged in the runs table.** Hard ceiling enforced in code.
17. **No new scoring systems, generated artifacts, or workflow stages may be added before the first vertical slice is built.** Ship Condition (see end of document).

---

## Build Sessions

The agent reads the entire spec before starting. The build is split into sessions. Each session has a fixed scope and a required check-in. Do not continue to the next session without explicit user approval.

### Session 1: Thin vertical slice, CSV-first

Goal: produce one usable outreach row end to end without requiring Attio.

Build only:
- `core/config_loader.py` (workspace-aware; built first)
- `core/db.py` (SQLAlchemy models, per-workspace db path)
- `core/http_client.py` (timeout, retry, rate limit)
- `core/llm/client.py` and `core/llm/limiter.py` (process-wide singleton)
- `core/runs.py` (run logging)
- `core/csv_export.py` (writes `clients/{workspace}/exports/review_queue.csv`)
- Stub versions of `core/verification.py`, `core/signal_quality.py`, `core/round_fit.py`, `core/lead_likelihood.py`, `core/similarity.py` (return canned values)
- Minimal `scripts/07_generate_emails.py` that reads one partner from a fixture and produces a draft using one founder example
- Pydantic schemas for partner_signals and email_generation

Input fixture:
- One manually supplied fund row
- One manually supplied partner row
- One manually pasted signal (with source URL)
- One company config in `clients/test_workspace/config/company.yaml`
- One founder example in `clients/test_workspace/prompts/examples/signal_led.md`

Output:
- One scored partner record in workspace SQLite
- One drafted email
- One deck-request response
- One follow-up draft
- One CSV row in `clients/test_workspace/exports/review_queue.csv`

Do not build Attio sync in Session 1. The CSV is the terminal artifact.

### Session 2: Stage 1 source aggregation + Stage 2 fund enrichment

Build only:
- `scripts/01_aggregate_sources.py`
- `scripts/02_enrich_funds.py`
- `schemas/fund_enrichment.py`
- `prompts/enrich_fund.txt`
- Source snapshot storage and content-hash deduping
- Partner discovery from team pages
- Deterministic `partner_id` generation: `slug(fund_domain + "_" + normalized_partner_name)`
- `clients/{workspace}/data/fixtures/funds_seed.csv` (5 hand-curated rows)

Run only:
- Stage 1 aggregation on fixture
- Stage 2 enrichment on the 5-fund fixture

### Session 3: Stage 3 recent activity mining

Build only:
- `scripts/03_mine_activity.py`
- `schemas/deal_attribution.py`
- `prompts/attribute_deal.txt`
- `clients/{workspace}/data/fixtures/announcements.json`

Run only:
- Stage 3 on fixture announcements

### Session 4: Stage 4 partner signal mining + cold reachability

Build only:
- `scripts/04_mine_partner_signals.py`
- `schemas/partner_signals.py`
- `prompts/extract_partner_signals.txt`
- Thesis signal extraction (per axis)
- Cold reachability signal extraction (in the same pass)
- Snapshot linking for every extracted signal

Run only:
- Stage 4 on the 10 fixture partners

**Important architectural change from v1:** Stage 4 does NOT extract round_fit or lead_likelihood from LLM-inferred public language. Both are computed in Stage 6 from observable facts. Stage 4 only produces: thesis signals (per axis) and cold reachability signals (limited LLM tail with observable primary).

### Session 5: Stage 5 verification + signal quality

Build only:
- `scripts/05_verify_and_quality.py`
- Live URL verification
- Quote substring matching with whitespace normalization
- Snapshot fallback verification with content_hash trust rule
- Verification error logging
- Signal quality scoring (0-3) using the shared calibration set at `core/calibration/signal_quality_examples.json`
- `schemas/signal_quality.py`
- `prompts/signal_quality.txt`

Run only:
- Stage 5 on signals from the fixture run

### Session 6: Stage 6 scoring + recommendation logic

Build only:
- `scripts/06_score_candidates.py`
- `schemas/candidate_score.py`
- `prompts/score_candidate.txt`
- `core/round_fit.py` — deterministic calculation (no LLM)
- `core/lead_likelihood.py` — mostly deterministic (named-as-lead counts; LLM only for explanatory text)
- 4-axis composite score (LLM, for thesis/personality fit only — NOT round eligibility)
- Axis spikiness fields (`axis_max_score`, `axis_score_variance`, `spiky_belief_score`)
- `send_now_priority` calculation (see Send Now Priority section)
- `partner_score_summaries` writes
- Major kill signal handling (hard) and soft kill warnings
- Full `recommended_to_send` calculation per the Recommended To Send section

Run only:
- Stage 6 on partners with verified, quality-≥2 fixture signals

### Session 7: Stage 7 email generation + CSV write

Build only:
- `scripts/07_generate_emails.py` (full version, replacing the Session 1 stub)
- `schemas/email_generation.py`
- `prompts/generate_email.txt`
- Strategy eligibility scoring (0-3) before selection
- Two-variant generation per partner with TWO DIFFERENT strategies (schema-enforced)
- Recommended variant selection with reasoning
- Conversion hypothesis generation per recommended variant
- Likely objection identification + preemption tagging
- Deck-request response generation per partner
- Follow-up draft generation per partner
- Similarity check across all recommended drafts in the batch (rapidfuzz token-set similarity, normalized 0.0-1.0)
- Template-smell LLM judge pass (sees 5 nearest neighbors, not random sample)
- Batch QA hard gates + warning gates
- Batch QA report writeback to SQLite
- Full CSV write to `clients/{workspace}/exports/review_queue.csv`

Run only:
- Stage 7 for the 5 highest-scored fixture partners
- Batch QA pass at the end with report output
- CSV review queue produced

### Session 8: Stage 8 Attio sync (optional path)

Build only:
- `scripts/00_verify_attio_schema.py`
- `scripts/08_sync_to_attio.py`
- `core/attio_client.py` (thin wrapper)
- Company upsert with `domains` matching
- Partner matching strategy (email, then LinkedIn URL query, then name plus company-link query)
- Company-person linking via `target_record_id`
- Preserve-on-outreach-started logic
- Manual override protection logic
- Attio sync logging

Run only:
- Sync the 5 fixture partners and their funds to Attio (if Attio is configured for the test workspace)

### Session 9: Background jobs

Build only:
- `jobs/attio_outcome_sync.py`
- `jobs/monthly_learning_report.py`
- `jobs/apply_axis_suggestion.py`

Run only:
- Outcome sync against a small test set (only if Attio configured)
- Monthly rescore against fixture outcomes (must produce suggestions, must not modify `config/axes.yaml`)

### Session 10: Scale to 50 (corresponds to Execution Gate 6)

Run the full pipeline on a broader source set and stop at 50 candidates. No new build.

### Session 11: Scale to 200 (corresponds to Execution Gate 7)

Run the full production batch. No new build. No automatic scale beyond this.

---

## Session Check-In Format

At the end of every Build Session, the agent must stop and respond in this exact format:

1. **What was built.** Files created or modified, with one-line purpose each.
2. **What commands were run.** Exact commands and their exit codes.
3. **What passed.** Tests, schema validations, smoke tests, fixture runs that succeeded.
4. **What failed or was skipped.** Anything that did not work, plus what was deliberately deferred.
5. **What assumptions were made.** Any decision the agent made that was not explicit in the spec.
6. **What files changed.** Diff summary by file.
7. **What user must verify before continuing.** Specific things the user should look at.
8. **Recommended next session.** Either the next numbered session, or "revisit Session X before continuing" if something earlier needs rework.

The agent must not proceed to the next session until the user explicitly says to continue.

---

## Execution Gates

**Gate 1 (corresponds to Session 1): Vertical slice produces a CSV row.**
- One partner end to end produces a CSV row with all required fields populated (even if from stubs).
- User opens the CSV and confirms structure is correct.
- Approval required to proceed.

**Gate 2 (Stage 1 + 2): 5 funds enriched.**
- Source aggregation produces 5 valid fund records on the fixture.
- Enrichment scrapes each fund site and produces structured records validated against `schemas/fund_enrichment.py`.
- User spot-checks each of the 5 records against the actual fund websites.
- Approval required to proceed.

**Gate 3 (Stage 3 + 4): 10 partners with extracted signals.**
- Activity mining finds at least 3 recent deals attributed to known funds.
- Partner signal mining finds at least 1 extracted (not yet verified) thesis signal per partner for at least 6 of 10 partners.
- User reviews the extracted signals: do the quotes sound like real partner language?
- Approval required to proceed.

**Gate 4 (Stage 5): At least 10 verified, quality-≥2 signals.**
- Verification gauntlet runs against all signals from Gate 3.
- At least 10 signals must pass URL resolution AND substring quote match (live or snapshot fallback) AND have `signal_quality_score >= 2`.
- If fewer than 10 pass, recalibrate Stage 4 prompts before scaling.
- User reviews verified vs unverified and quality-distribution counts.
- Approval required to proceed.

**Gate 5 (Stage 6 + 7): Emails for 5 partners.**
- Stage 6 scores partners using only verified, quality-≥2 signals plus deterministic round_fit and lead_likelihood.
- Stage 7 generates emails for the 5 highest-scored partners by `send_now_priority`.
- Two variants per partner using TWO DIFFERENT strategies, plus deck_request_response, follow-up, and conversion hypothesis. All validated against `schemas/email_generation.py`.
- Batch QA pass runs: similarity, template-smell, hard gates.
- User reads all 10 drafts plus 5 deck responses, 5 follow-ups, 5 hypotheses, and grades against the sendable rubric.

Sendable rubric (each draft must satisfy ALL):

- Sounds like the founder would write it
- Contains exactly one genuinely specific investor signal
- Connects the company to that signal without forcing the link
- Makes the company feel investable in one sentence
- Explicitly references the active raise
- Asks for a pitch meeting directly with a concrete next step
- Does NOT soften the ask into "thesis chat", "feedback", "pressure-test", "compare notes"
- Does not read as "I researched you with AI"
- Does not include raw URLs in the body
- Does not include any phrase from `founder_voice.banned_phrases`

Batch QA rubric for Gate 5 (10-draft fixture batch, qualitative):

- At least 3 different strategies attempted across the 10 drafts where partner evidence supports them
- Any partner with `limited_variation=true` has a documented reason
- No two drafts have body similarity above 0.82
- No two drafts have first-sentence similarity above 0.70
- At least 70 percent of drafts have `template_smell=low` (relaxed from production threshold due to small sample)

If the fixture batch fails any QA rubric item, the prompt or evidence inputs need refinement before scaling.

Approval required to proceed.

**Gate 5.5: Calibration batch (mandatory before scaling).**
- 8 to 10 partners selected from the mid-priority tier (high fit, but NOT the highest-priority targets)
- Mix of 3 to 4 strategies across the batch
- User manually reviews each draft against the sendable rubric
- User SENDS the calibration emails (first real-world data point)
- Wait at least 5 business days for outcomes

Calibration outcomes (sharpened thresholds):

- **Green**: 2+ meetings booked from 8-10 sends, OR 1 meeting plus 2+ substantive replies from relevant partners
- **Yellow**: 1 meeting booked, OR 2+ substantive replies without a meeting
- **Red**: no meaningful replies, generic passes only, or replies indicating wrong stage/category

If Green: proceed to top 25.
If Yellow: revise prompts/examples/company.yaml once, run one more calibration batch on different mid-tier partners.
If Red: do not scale to top targets. Iterate on `prompts/examples/`, `company.yaml` (especially `round_hook` and `strongest_raise_proof`), or the email prompt instructions, then re-run calibration on a new batch.
If Red twice: do not use cold pipeline for this raise beyond manual one-offs. The bottleneck is upstream of email mechanics.

Reasoning: the highest-priority 25 partners are the most expensive cold shots available. Burning them on an unvalidated email approach is a strategic error. The calibration batch costs 8-10 mid-priority sends; the information return is enormous.

**Gate 6 (Session 10): Scale to 50.**
- Full pipeline runs on top 50 candidates after broader sourcing.
- User reviews 5 randomly sampled records end to end.
- Approval required to proceed.

**Gate 7 (Session 11): Scale to 200.**
- Full pipeline runs on the complete target universe of approximately 200.
- This is the production batch. User reviews the top 20 by composite score.
- No further automatic scale-up.

---

## CSV-First Output: review_queue.csv

The primary deliverable per batch. Written by Stage 7 at `clients/{workspace}/exports/review_queue.csv`. Columns:

| Column | Source |
|---|---|
| partner_id | `partners.partner_id` (deterministic slug) |
| partner_name | `partners.name` |
| partner_title | `partners.title` |
| fund_name | `funds.name` |
| fund_domain | `funds.domain` |
| linkedin_url | `partners.linkedin_url` |
| send_now_priority | `partner_score_summaries.send_now_priority` |
| composite_fit_score | `partner_score_summaries.composite_fit_score` |
| round_fit_score | `partner_score_summaries.round_fit_score` (deterministic) |
| round_fit_reasoning | `partner_score_summaries.round_fit_reasoning` |
| lead_likelihood_score | `partner_score_summaries.lead_likelihood_score` (deterministic) |
| lead_likelihood_signals | `partner_score_summaries.lead_likelihood_signals` |
| cold_reachability_score | `partner_score_summaries.cold_reachability_score` |
| spiky_belief_score | `partner_score_summaries.spiky_belief_score` |
| top_signals | top 3 verified, quality-≥2 signals with URLs and dates |
| recommended_to_send | `partner_score_summaries.recommended_to_send` |
| recommendation_reasoning | `partner_score_summaries.recommendation_reasoning` |
| email_strategy_used | recommended variant strategy |
| email_subject_line | recommended variant subject |
| outreach_email_draft | recommended variant body |
| conversion_hypothesis | recommended variant hypothesis |
| likely_objection | recommended variant likely objection |
| objection_preempted | true/false |
| email_alternate_strategy | alternate variant strategy |
| email_draft_alternate | alternate variant body |
| followup_email_draft | follow-up draft |
| deck_request_response | deck deflection reply |
| template_smell | low/medium/high |
| warm_path_available | from manual flag if set |
| outreach_status | initial state: `ready_to_send` if `recommended_to_send=TRUE`, else `draft` |

The CSV is overwritten on each Stage 7 run. The SQLite database retains historical batches.

---

## Attio setup (optional)

The system writes to two Attio objects when configured: `companies` (one record per fund) and `people` (one record per partner). It uses Attio's standard objects, extended with custom attributes.

If Attio is not used, skip this section entirely. The CSV path works without it.

### Attio API assumptions to verify in Stage 0

Stage 0 must smoke-test each before any real sync runs.

- Base URL: `https://api.attio.com/v2`
- Authentication: `Authorization: Bearer {ATTIO_API_KEY}` (from workspace `.env`)
- Object slugs for standard objects: `companies` and `people`
- Upsert endpoint: `PUT /v2/objects/{object}/records` (asserts by matching attribute, e.g., `domains` for companies, `email_addresses` for people)
- Create endpoint: `POST /v2/objects/{object}/records`
- Update endpoint: `PATCH /v2/objects/{object}/records/{record_id}`
- Query endpoint: `POST /v2/objects/{object}/records/query`
- People natively have a `company` attribute that takes `{target_object: "companies", target_record_id: "..."}` for linking
- Required OAuth scopes: `record_permission:read-write` and `object_configuration:read`

Stage 0 performs one create-or-upsert against a known throwaway company and person record (with cleanup) to confirm payload shapes. If any assumption is wrong, fail fast with a clear error.

### Custom attributes on the `companies` object

| Attribute name | API slug | Type | Purpose |
|---|---|---|---|
| Fund Thesis Summary | `fund_thesis_summary` | Long text | One-sentence thesis in fund's own language |
| Stage Focus | `stage_focus` | Select (pre-seed, seed, series-a, multi-stage) | Stage they invest at |
| Check Size Range | `check_size_range` | Text | As stated by fund |
| Last Known Activity | `last_known_activity_date` | Date | Most recent observed deal |
| Active Investor | `is_active_investor` | Checkbox | True if activity in last 12 months |
| Kill Signals | `kill_signals` | Long text | Reasons not to contact |

### Custom attributes on the `people` object

| Attribute name | API slug | Type | Purpose |
|---|---|---|---|
| Composite Fit Score | `composite_fit_score` | Number | 0 to 10, thesis/personality fit only |
| Axis 1 Score | `axis_1_score` | Number | 0 to 10 |
| Axis 2 Score | `axis_2_score` | Number | 0 to 10 |
| Axis 3 Score | `axis_3_score` | Number | 0 to 10 |
| Axis 4 Score | `axis_4_score` | Number | 0 to 10 |
| Axis Max Score | `axis_max_score` | Number | highest of the 4 |
| Axis Score Variance | `axis_score_variance` | Number | variance across the 4 |
| Spiky Belief Score | `spiky_belief_score` | Number | 0 to 2 bonus |
| Score Confidence | `score_confidence` | Select (low, medium, high) | Based on signal volume |
| Top Signals | `top_signals` | Long text | Top 3 verified, quality-≥2 quotes with URLs and dates |
| Last Signal Date | `last_signal_date` | Date | Recency of most recent signal |
| Partner Kill Signals | `partner_kill_signals` | Long text | Anti-cold-outreach statements, transitions |
| Cold Reachability Score | `cold_reachability_score` | Number | 0 to 10 |
| Reachability Signals | `reachability_signals` | Long text | Evidence supporting reachability score |
| Round Fit Score | `round_fit_score` | Number | 0 to 10, deterministic |
| Round Fit Reasoning | `round_fit_reasoning` | Long text | One sentence on why partner fits or does not fit this raise |
| Lead Likelihood Score | `lead_likelihood_score` | Number | 0 to 10, mostly deterministic |
| Lead Likelihood Signals | `lead_likelihood_signals` | Long text | Evidence supporting score |
| Send Now Priority | `send_now_priority` | Number | Composite ranking for ordering this week's sends |
| Outreach Email Draft | `outreach_email_draft` | Long text | Recommended email ready for review |
| Email Strategy Used | `email_strategy_used` | Select (6 strategies) | Strategy of recommended variant |
| Conversion Hypothesis | `conversion_hypothesis` | Long text | One-sentence reasoning |
| Likely Objection | `likely_objection` | Long text | Most likely objection |
| Objection Preempted | `objection_preempted` | Checkbox | Whether body addresses it |
| Template Smell | `template_smell` | Select (low, medium, high, unscored) | Output of batch QA judge |
| Email Subject Line | `email_subject_line` | Text | Recommended subject |
| Email Draft Alternate | `email_draft_alternate` | Long text | The other strategy variant |
| Alternate Strategy | `email_alternate_strategy` | Select (6 strategies) | Strategy of alternate |
| Follow-up Email Draft | `followup_email_draft` | Long text | 2-sentence follow-up |
| Deck Request Response | `deck_request_response` | Long text | Pre-drafted deck deflection |
| Outreach Status | `outreach_status` | Select (draft, ready_to_send, sent, replied, meeting_booked, dead, warm_path_needed) | Pipeline stage |
| Meeting Booked | `meeting_booked` | Checkbox | True when pitch meeting on calendar |
| Meeting Date | `meeting_date` | Date | When the pitch happens |
| Meeting Outcome | `meeting_outcome` | Select (pitched, no_show, advanced, killed, pending) | Result |
| Recommended To Send | `recommended_to_send` | Checkbox | System recommendation |
| Manual Score Override | `manual_score_override` | Checkbox | TRUE if user manually edited any score |
| Manual Recommendation Override | `manual_recommended_override` | Checkbox | TRUE if user manually toggled recommended_to_send |
| Manual Override Reason | `manual_override_reason` | Long text | One-line note |
| Warm Path Available | `warm_path_available` | Checkbox | User-set; if TRUE, do not cold email |
| Warm Path Contact | `warm_path_contact` | Text | Optional note |
| Reply Type | `reply_type` | Select (no_response, booked, asked_for_deck, passed_too_early, passed_category, wrong_stage, asked_for_more_info, referred_to_colleague, warm_intro_requested) | Categorized outcome |

### Recommended default view configuration

With ~25 custom attributes per partner, configure the default people view to show only:

- Name, Title, Company (standard)
- Send Now Priority
- Composite Fit Score
- Round Fit Score
- Outreach Status
- Outreach Email Draft
- Email Strategy Used
- Conversion Hypothesis
- Deck Request Response
- Meeting Booked

Move alternate email, alternate strategy, follow-up draft, axis-by-axis scores, raw signals, and verification metadata to a secondary "Pipeline Detail" view.

---

## Configuration

See PROJECT_BRIEF source for full config templates (company.yaml, axes.yaml, attio.yaml, sources.yaml).

---

## Recommended To Send: calculation logic

A partner is set `recommended_to_send=TRUE` only when ALL of the following hold:

1. `composite_fit_score >= 6.5` (thesis fit threshold)
2. `round_fit_score >= 6.0` AND no `round_fit` disqualifier present (deterministic)
3. `lead_likelihood_score >= 5.0` OR `lead_likelihood_score IS NULL`
4. At least 2 distinct evidence sources verified at quality ≥2
5. At least one verified quality-≥2 evidence item dated within the last 18 months
6. Partner's current fund employment is `verified_current` or `likely_current`
7. No major kill signal present
8. `cold_reachability_score >= 5` OR `cold_reachability_score IS NULL`
9. `warm_path_available != TRUE`
10. At least one strategy in Stage 7 strategy eligibility scoring >= 2

See full PROJECT_BRIEF source for employment confidence levels, warm path override, operational kill signals, soft kill signals, email strategy, batch QA, background jobs, pydantic schemas, prompts, acceptance criteria, calibration outcomes, and ship condition.
