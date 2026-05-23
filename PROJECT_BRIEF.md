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

### `clients/{workspace}/config/company.yaml`

```yaml
company:
  name: "{COMPANY_NAME}"
  founder_name: "{FOUNDER_NAME}"
  founder_email: "{FOUNDER_EMAIL}"
  one_liner: "{ONE_SENTENCE_DESCRIPTION}"
  description: |
    {LONGER_PARAGRAPH_DESCRIPTION_INCLUDING_TRACTION_AND_CATEGORY}
  current_traction:
    headline_metric: "{e.g., $X ARR, X paying customers}"
    secondary_metrics:
      - "{e.g., NRR 130%}"
      - "{e.g., 4 design partners signed}"
  stage: "{PRE_SEED|SEED|SERIES_A}"
  target_check_size_usd:
    min: {MIN_CHECK}
    max: {MAX_CHECK}
  target_geographies:
    - "{GEO_1}"
  adjacent_companies:
    - "{ADJACENT_CO_1}"
  anchor_funds:
    - "{ANCHOR_FUND_1}"
  meeting_ask:
    duration_minutes: 30
    format: "video call"
    preferred_scheduling_link: "{CALENDLY_OR_OTHER_URL}"

# Mandatory. The system only operates because this is true.
raise_context:
  round: "{Seed | Series A | etc.}"
  amount: "{TARGET_RAISE_OR_OMIT_IF_SENSITIVE}"
  instrument: "{priced | SAFE | convertible note | TBD}"
  status: "{opening conversations | actively in market | first checks soft-circled | allocation remaining}"
  timing: "{target first close date / final close date / decision window}"
  use_of_funds:
    - "{milestone 1 this round funds}"
    - "{milestone 2 this round funds}"
  why_this_round_is_fundable_now: "{One sentence}"
  what_changes_after_this_round: "{One sentence}"
  strongest_raise_proof: "{Founder-designated best proof. System defaults to this unless partner signals suggest otherwise.}"
  round_hook:
    strongest_reason_to_meet_now: "{One sentence}"
    investor_consequence_of_waiting: "{What allocation, timing, or evidence they miss if they wait}"
    round_momentum_proof: "{soft-circled checks, customer milestone, pilot timing, strategic interest, etc.}"
  notable_existing_investors_or_non_dilutive: "{Optional}"

# Round-fit gating. Computed deterministically in Stage 6.
round_fit:
  must_have:
    - "invests at this stage"
    - "can write or lead target check size"
    - "has made at least one new investment in last 12 to 18 months"
  nice_to_have:
    - "has led comparable rounds"
    - "has reserves for follow-on"
    - "partner-level conviction in category"
  disqualifiers:
    - "growth-only investor"
    - "pre-seed-only when raising seed (or seed-only when raising A)"
    - "follow-on capital only, never leads"
    - "not currently deploying capital"
    - "check size constraint mismatched to this round"

# Founder voice for email drafting. Examples in prompts/examples/ are the binding artifact.
founder_voice:
  style: "{e.g., direct, serious, high-conviction, not hypey. Few words, no buzzwords.}"
  banned_phrases:
    - "would love"
    - "excited to"
    - "game-changing"
    - "synergy"
  preferred_phrases:
    - "{phrases the founder naturally uses}"
  example_emails_path: "prompts/examples/"
```

### `clients/{workspace}/config/axes.yaml`

```yaml
# Each axis must describe investor PSYCHOLOGY or BELIEF, not a sector label.
#
# Bad axis: "fintech fit"
# Why bad: every fintech investor scores 10. Does not discriminate.
#
# Good axis: "believes regulated-market wedges beat pure consumer adoption"
# Why good: some fintech investors believe this, many do not. Discriminates.
#
# Test for orthogonality: if any plausible investor would always score together
# across two axes, collapse them. Different axes must measure different beliefs.
#
# Round eligibility is NOT an axis. Round fit is computed deterministically in Stage 6.
# These axes measure thesis and personality fit only.
axes:
  - id: axis_1
    name: "{AXIS_1_NAME}"
    description: "{ONE_SENTENCE_DESCRIPTION_OF_THE_BELIEF_OR_MINDSET}"
    positive_signals:
      - "{KEYWORD_OR_PHRASE_1}"
      - "{KEYWORD_OR_PHRASE_2}"
    negative_signals:
      - "{ANTI_SIGNAL_1}"
    weight: 1.0
  # repeat for axes 2, 3, 4
```

### `clients/{workspace}/config/attio.yaml` (optional)

```yaml
attio:
  workspace_id: "{ATTIO_WORKSPACE_ID}"
  api_base: "https://api.attio.com/v2"
  matching_attributes:
    companies: "domains"
    people: "email_addresses"
  objects:
    funds: "companies"
    partners: "people"
  fund_attributes:
    fund_thesis_summary: "fund_thesis_summary"
    stage_focus: "stage_focus"
    check_size_range: "check_size_range"
    last_known_activity_date: "last_known_activity_date"
    is_active_investor: "is_active_investor"
    kill_signals: "kill_signals"
  partner_attributes:
    # full list as per Attio attribute table above; mapped 1:1 by API slug
    composite_fit_score: "composite_fit_score"
    axis_1_score: "axis_1_score"
    axis_2_score: "axis_2_score"
    axis_3_score: "axis_3_score"
    axis_4_score: "axis_4_score"
    axis_max_score: "axis_max_score"
    axis_score_variance: "axis_score_variance"
    spiky_belief_score: "spiky_belief_score"
    score_confidence: "score_confidence"
    top_signals: "top_signals"
    last_signal_date: "last_signal_date"
    partner_kill_signals: "partner_kill_signals"
    cold_reachability_score: "cold_reachability_score"
    reachability_signals: "reachability_signals"
    round_fit_score: "round_fit_score"
    round_fit_reasoning: "round_fit_reasoning"
    lead_likelihood_score: "lead_likelihood_score"
    lead_likelihood_signals: "lead_likelihood_signals"
    send_now_priority: "send_now_priority"
    outreach_email_draft: "outreach_email_draft"
    email_strategy_used: "email_strategy_used"
    conversion_hypothesis: "conversion_hypothesis"
    likely_objection: "likely_objection"
    objection_preempted: "objection_preempted"
    template_smell: "template_smell"
    email_subject_line: "email_subject_line"
    email_draft_alternate: "email_draft_alternate"
    email_alternate_strategy: "email_alternate_strategy"
    followup_email_draft: "followup_email_draft"
    deck_request_response: "deck_request_response"
    outreach_status: "outreach_status"
    meeting_booked: "meeting_booked"
    meeting_date: "meeting_date"
    meeting_outcome: "meeting_outcome"
    recommended_to_send: "recommended_to_send"
    manual_score_override: "manual_score_override"
    manual_recommended_override: "manual_recommended_override"
    manual_override_reason: "manual_override_reason"
    warm_path_available: "warm_path_available"
    warm_path_contact: "warm_path_contact"
    reply_type: "reply_type"
  list_id: "{ATTIO_LIST_ID_OPTIONAL}"
  preserve_on_outreach_started:
    statuses: ["sent", "replied", "meeting_booked", "dead"]
    preserved_fields:
      - "outreach_email_draft"
      - "email_subject_line"
      - "email_strategy_used"
      - "conversion_hypothesis"
      - "likely_objection"
      - "objection_preempted"
      - "email_draft_alternate"
      - "email_alternate_strategy"
      - "followup_email_draft"
      - "deck_request_response"
      - "template_smell"
  manual_override_protection:
    if_manual_score_override_true_preserve:
      - "composite_fit_score"
      - "axis_1_score"
      - "axis_2_score"
      - "axis_3_score"
      - "axis_4_score"
      - "axis_max_score"
      - "axis_score_variance"
      - "spiky_belief_score"
      - "round_fit_score"
      - "lead_likelihood_score"
      - "cold_reachability_score"
      - "send_now_priority"
    if_manual_recommended_override_true_preserve:
      - "recommended_to_send"
```

### `clients/{workspace}/config/sources.yaml`

```yaml
public_lists:
  - name: "OpenVC Export"
    path: "data/raw/openvc_export.csv"
    parser: "csv"
  - name: "GitHub Awesome List"
    url: "{URL_OF_RAW_MARKDOWN_FROM_GITHUB}"
    parser: "markdown"
funding_announcement_feeds:
  - name: "TechCrunch Funding"
    url: "https://techcrunch.com/category/venture/feed/"
    parser: "rss"
  - name: "Crunchbase News"
    url: "https://news.crunchbase.com/feed/"
    parser: "rss"
partner_signal_sources:
  podcast_search_api: "listennotes"
  substack_search: true
  twitter_handles_file: "data/raw/partner_twitter_handles.csv"
```

OpenVC: log in at openvc.app (free), filter the investor database, export to CSV through the platform UI. Save to `clients/{workspace}/data/raw/openvc_export.csv`.

---

## Client Onboarding Requirement

Before email generation can produce `ready_to_send` drafts, the founder must provide a minimum example set in `clients/{workspace}/prompts/examples/`:

- 3 signal-led examples (`signal_led.md`)
- 3 portfolio-led or market-shift examples (`portfolio_led.md` and/or `market_shift_led.md`)
- 2 follow-up examples (`follow_up.md`)
- 2 deck-request response examples (`deck_request_response.md`)

These examples define the target founder voice. If the founder does not have a stable cold-email voice yet, this exercise creates it.

If example files are missing for a strategy, Stage 7 either skips that strategy for the partner or generates with no style anchor and warns. Strategies without examples will produce lower-quality drafts; the system warns rather than blocks, but the quality drop is visible at Gate 5.

This is per-client homework. Voice extraction from existing sent emails is not built in v1.

---

## Cross-Workspace Learning Boundary

By default, outcome learning is workspace-local.

No client-specific partner data, emails, outcomes, or reply details are used to improve another workspace.

Optional cross-workspace learning may be enabled only when the user opts in via a flag in the workspace's `attio.yaml` (or a separate `learning.yaml`). When enabled, only anonymized aggregate statistics may be shared across workspaces, such as:

- strategy type → reply rate
- strategy type → meeting rate
- follow-up timing → reply rate
- template-smell bucket → reply rate
- cold reachability bucket → reply rate

**Never share across workspaces:**

- partner names
- fund names
- email text (subject or body)
- company names
- founder examples
- raw signals
- deal-specific notes
- outcome notes

Cross-workspace aggregates live in `core/cross_workspace_stats.json`, written only by `jobs/monthly_learning_report.py` and only when at least one workspace has opted in.

---

## Manual Override and Force Refresh

Manual overrides protect user judgment from being overwritten by routine syncs.

If `manual_score_override=TRUE` or `manual_recommended_override=TRUE` on an Attio record (or the equivalent in workspace SQLite), pipeline runs do not overwrite the protected field.

A user may intentionally refresh an overridden record using:

```bash
uv run scripts/06_score_candidates.py --workspace clients/{name} \
  --force-rescore --reason "new fund/team/deal evidence" \
  --partner-id {partner_id}
```

Every forced overwrite is logged with:
- field changed
- old value
- new value
- reason
- timestamp

`--force-rescore` is per-record (or per-field), never global. There is no `--force-rescore-all` flag.

---

## SQLite schema (per workspace)

```sql
CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    stage TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    records_processed INTEGER,
    records_succeeded INTEGER,
    records_failed INTEGER,
    records_skipped INTEGER,
    llm_calls_made INTEGER,
    llm_input_tokens INTEGER,
    llm_output_tokens INTEGER,
    estimated_cost_usd REAL,
    elapsed_seconds INTEGER,
    error_summary TEXT
);

CREATE TABLE run_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(run_id),
    record_id TEXT,
    error_type TEXT,
    error_message TEXT,
    occurred_at TIMESTAMP
);

CREATE TABLE force_refresh_log (
    refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    refreshed_at TIMESTAMP
);

CREATE TABLE funds (
    fund_id TEXT PRIMARY KEY,
    attio_record_id TEXT,
    name TEXT NOT NULL,
    domain TEXT,
    stated_thesis TEXT,
    stated_stage_focus TEXT,
    check_size_range TEXT,
    last_known_activity_date DATE,
    is_active BOOLEAN,
    kill_signals TEXT,
    source_urls TEXT,
    last_updated TIMESTAMP
);

CREATE TABLE partners (
    partner_id TEXT PRIMARY KEY,
    attio_record_id TEXT,
    fund_id TEXT REFERENCES funds(fund_id),
    name TEXT NOT NULL,
    title TEXT,
    linkedin_url TEXT,
    twitter_handle TEXT,
    bio TEXT,
    employment_status TEXT DEFAULT 'uncertain',
    employment_verification_source_urls TEXT,
    employment_verification_date DATE,
    warm_path_available BOOLEAN DEFAULT NULL,
    warm_path_contact TEXT,
    last_updated TIMESTAMP
);

CREATE TABLE source_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    http_status INTEGER,
    content_hash TEXT,
    extracted_text TEXT,
    fetched_during_stage TEXT
);

CREATE INDEX idx_snapshots_url_hash ON source_snapshots(source_url, content_hash);

CREATE TABLE signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT REFERENCES partners(partner_id),
    snapshot_id INTEGER REFERENCES source_snapshots(snapshot_id),
    source_type TEXT,
    source_url TEXT NOT NULL,
    quoted_text TEXT NOT NULL,
    quote_date DATE,
    axis_relevance TEXT,
    signal_direction TEXT,
    verified BOOLEAN DEFAULT FALSE,
    verification_method TEXT,
    verification_error TEXT,
    signal_quality_score INTEGER,
    quality_reasoning TEXT,
    captured_at TIMESTAMP
);

CREATE TABLE deal_attributions (
    deal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    round_type TEXT,
    round_size_usd INTEGER,
    announcement_date DATE,
    lead_fund_id TEXT REFERENCES funds(fund_id),
    attributed_partner_id TEXT REFERENCES partners(partner_id),
    source_url TEXT,
    captured_at TIMESTAMP
);

CREATE TABLE scores (
    partner_id TEXT REFERENCES partners(partner_id),
    axis_id TEXT,
    score REAL,
    supporting_signal_ids TEXT,
    confidence TEXT,
    scored_at TIMESTAMP,
    PRIMARY KEY (partner_id, axis_id, scored_at)
);

CREATE TABLE partner_score_summaries (
    partner_id TEXT PRIMARY KEY REFERENCES partners(partner_id),
    composite_fit_score REAL,
    axis_max_score REAL,
    axis_score_variance REAL,
    spiky_belief_score REAL,
    score_confidence TEXT,
    verified_signal_count INTEGER,
    quality_2_plus_signal_count INTEGER,
    distinct_source_type_count INTEGER,
    most_recent_signal_date DATE,
    major_kill_signal_present BOOLEAN,
    kill_signal_summary TEXT,
    cold_reachability_score REAL,
    round_fit_score REAL,
    round_fit_reasoning TEXT,
    lead_likelihood_score REAL,
    lead_likelihood_signals TEXT,
    send_now_priority REAL,
    employment_status TEXT,
    manual_score_override BOOLEAN DEFAULT FALSE,
    manual_recommended_override BOOLEAN DEFAULT FALSE,
    manual_override_reason TEXT,
    recommended_to_send BOOLEAN,
    recommendation_reasoning TEXT,
    scored_at TIMESTAMP
);

CREATE TABLE email_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT REFERENCES partners(partner_id),
    batch_id TEXT,
    strategy TEXT,
    subject TEXT,
    body TEXT,
    conversion_hypothesis TEXT,
    likely_objection TEXT,
    objection_preempted BOOLEAN,
    preemption_line TEXT,
    template_smell TEXT DEFAULT 'unscored',
    qa_status TEXT DEFAULT 'unscored',
    regeneration_count INTEGER DEFAULT 0,
    is_recommended BOOLEAN,
    generated_at TIMESTAMP,
    pushed_to_attio_at TIMESTAMP,
    written_to_csv_at TIMESTAMP
);

CREATE TABLE followup_drafts (
    followup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT REFERENCES partners(partner_id),
    body TEXT,
    generated_at TIMESTAMP,
    pushed_to_attio_at TIMESTAMP
);

CREATE TABLE deck_request_responses (
    response_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT REFERENCES partners(partner_id),
    body TEXT,
    generated_at TIMESTAMP,
    pushed_to_attio_at TIMESTAMP
);

CREATE TABLE batch_qa_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    batch_size INTEGER,
    strategy_distribution TEXT,
    similarity_failures INTEGER,
    template_smell_high_count INTEGER,
    raise_reference_missing_count INTEGER,
    passed BOOLEAN,
    failure_reasons TEXT,
    generated_at TIMESTAMP
);

CREATE TABLE attio_sync_log (
    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT,
    local_id TEXT,
    attio_record_id TEXT,
    operation TEXT,
    success BOOLEAN,
    error_message TEXT,
    synced_at TIMESTAMP
);

CREATE TABLE outcomes (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT REFERENCES partners(partner_id),
    outreach_status TEXT,
    reply_type TEXT,
    meeting_booked BOOLEAN,
    meeting_date DATE,
    meeting_outcome TEXT,
    synced_from_attio_at TIMESTAMP
);

CREATE TABLE axis_weight_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TIMESTAMP,
    axis_id TEXT,
    current_weight REAL,
    suggested_weight REAL,
    reason TEXT,
    confidence TEXT,
    sample_size INTEGER,
    approved BOOLEAN DEFAULT NULL,
    approved_at TIMESTAMP
);
```

---

## Build order with stage details

### Stage 0: verify Attio schema (optional path)

Script: `scripts/00_verify_attio_schema.py`

Connects to Attio if `clients/{workspace}/config/attio.yaml` exists. Reads expected attribute slugs. For each, checks existence via `GET /v2/objects/{object}/attributes`. If any missing, prints the list and exits non-zero. Never auto-creates attributes.

Runs as a precondition only if Attio sync is requested (`--with-attio` flag on Stage 8).

### Stage 1: aggregate sources

Reads `clients/{workspace}/config/sources.yaml`. Pulls each source. Normalizes to canonical schema. Dedupes by domain. Inserts into `funds` table.

Fixture: 5 hand-curated rows in `clients/{workspace}/data/fixtures/funds_seed.csv`.

Done when: 5 funds exist with valid domains, all parse without errors.

### Stage 2: enrich funds

For each fund with a domain, fetches homepage, `/portfolio`, `/team`, `/thesis`, `/about`, `/news`. Parses with selectolax. Every successfully fetched page is written to `source_snapshots` with extracted text and a content hash. Calls `prompts/enrich_fund.txt`. Output validated against `schemas/fund_enrichment.py`.

For each partner discovered on `/team` pages, derive `partner_id = slug(fund_domain + "_" + normalized_partner_name)` where normalization lowercases, strips whitespace, and removes punctuation.

Rate limit: max 5 concurrent requests, 1-second per-domain delay, rotating user agent.

Done when: at least 3 of 5 funds produce well-formed enriched records, and every fetched page has a corresponding snapshot row.

### Stage 3: mine recent activity

Pulls RSS feeds from `sources.yaml` for the last 12 months. For each funding announcement, runs `prompts/attribute_deal.txt`. Validated against `schemas/deal_attribution.py`. Updates `funds.last_known_activity_date` and `funds.is_active`. Attributes deals to specific partners where the announcement names them.

Fixture: 20 recent announcements at `clients/{workspace}/data/fixtures/announcements.json`.

Done when: at least 3 deals attributed to specific partners.

### Stage 4: mine partner-level signals

For each partner at an active fund, queries Listen Notes (if configured), Substack search, personal blogs from `/team`, and Twitter (if API access available). Every fetched page is written to `source_snapshots`. Runs `prompts/extract_partner_signals.txt`. Validated against `schemas/partner_signals.py`. Inserts into `signals` table with `verified=FALSE`.

The prompt extracts two types of evidence:

1. **Thesis signals**: quotes mapping to the 4 axes (psychology/belief, not sector). Exact quotes only.
2. **Cold reachability signals**: evidence on whether this partner takes cold inbound. Produces a partial `cold_reachability_score`. The final reachability score in Stage 6 combines this with deterministic checks (recent public output count, time since last public post).

**Important: Stage 4 does NOT produce round_fit or lead_likelihood scores.** Those are deterministic in Stage 6.

Done when: at least 6 of 10 fixture partners have at least 1 thesis signal extracted with all required fields including snapshot_id.

### Stage 5: verification gauntlet + signal quality scoring

Script: `scripts/05_verify_and_quality.py`

For every signal with `verified=FALSE`:

**Provenance verification:**

- Fetch the source URL. On HTTP error or timeout, attempt fallback to the source_snapshot taken at extraction time. If snapshot match also fails, mark `verification_method=url_failed`, log error.
- If URL resolves, fetch full content and substring-match the `quoted_text` (whitespace normalized). If no match against live content, attempt snapshot fallback. If snapshot also fails, mark `verification_method=quote_not_found`.
- If live content matches: mark `verified=TRUE`, `verification_method=live_match`.
- If only snapshot matches: mark `verified=TRUE`, `verification_method=snapshot_fallback`. Snapshot fallback is valid only if the snapshot was captured before or during signal extraction and its `content_hash` matches the text passed to the LLM at extraction time.

**Signal quality scoring (in the same script, for each verified signal):**

Calls `prompts/signal_quality.txt` with the verified quote, the axis it was tagged against, the company description, and the shared calibration examples from `core/calibration/signal_quality_examples.json`.

Score scale:

- 0 = verified but meaningless (e.g., generic affirmation like "yeah, infrastructure is interesting")
- 1 = generic category mention (the partner referenced the category but not in a way that signals belief or behavior)
- 2 = relevant but broad (the partner expressed a view on the category that suggests fit but does not specifically map to your company's wedge)
- 3 = specific, recent, and predictive of investor fit for this raise

Validated against `schemas/signal_quality.py`. Writes `signal_quality_score` and `quality_reasoning` to the `signals` table.

**Downstream gates:**

- Only `signal_quality_score >= 2` may support Stage 6 scoring.
- Only `signal_quality_score >= 3` may be used as the primary email opener in Stage 7.
- Lower-quality signals remain in the database for audit but are excluded downstream.

Done when: verification pass rate is between 50 and 80 percent AND at least 10 signals have `signal_quality_score >= 2`. Outside this range, recalibrate Stage 4 prompts before scaling.

### Stage 6: score candidates

For each partner with at least one verified, quality-≥2 signal:

**Step 1: 4-axis composite score (LLM).**

Runs `prompts/score_candidate.txt` against verified quality-≥2 signals only. Validated against `schemas/candidate_score.py`. Inserts per-axis scores into `scores` table.

The 4-axis composite measures THESIS AND PERSONALITY fit only. It does not measure round eligibility (handled deterministically below).

Computes:
- `composite_fit_score = sum(axis_scores * axis_weights) / sum(axis_weights)`
- `axis_max_score = max(axis_scores)`
- `axis_score_variance = variance(axis_scores)`
- `spiky_belief_score = clamp(axis_score_variance * 0.5, 0, 2)`

The `spiky_belief_score` rewards partners with sharp conviction on a subset of axes vs generalist mid-fit.

**Step 2: deterministic round_fit (no LLM).**

`core/round_fit.py` computes `round_fit_score` from observable facts only:

```
stage_match           = 0 or 3   (does fund.stated_stage_focus match raise_context.round)
check_size_match      = 0 or 2   (does fund.check_size_range overlap target_check_size_usd)
active_fund           = 0 or 2   (has fund deployed in last 18 months)
recent_relevant_deals = 0 to 2   (count of deals in adjacent sector in last 18 months)
partner_decision_power = 0 or 1  (is partner a GP/MD or equivalent, not associate/analyst)

round_fit_score = sum of above (max 10)
disqualifier_present = any of round_fit.disqualifiers true → round_fit_score capped at 2
```

`round_fit_reasoning` is generated by a SHORT LLM call summarizing the deterministic result in one sentence. The LLM never invents the score; it only describes it.

**Step 3: deterministic lead_likelihood (no LLM).**

`core/lead_likelihood.py` computes `lead_likelihood_score`:

```
named_as_lead_count  = number of deals in last 24 months where partner is named lead investor (from deal_attributions)
recent_board_seats   = count of board seats at portfolio companies in last 24 months (from fund site)
solo_check_pattern   = 0 or 2 (does partner have any deals where they are sole named investor)
title_seniority      = 0 to 2 (GP/MD = 2, principal = 1, associate = 0)
follow_on_only_flag  = -3 (set if all recent attributed deals are follow-ons)

lead_likelihood_score = clamp(named_as_lead_count * 2 + recent_board_seats + solo_check_pattern + title_seniority + follow_on_only_flag, 0, 10)
```

`lead_likelihood_signals` is a JSON list of the underlying evidence rows.

**Step 4: cold_reachability_score.**

Combines deterministic checks (count of public posts in last 12 months, time since last post, presence/absence of cold-inbound contact info on fund site) with the partial score from Stage 4. Capped at 0-10.

**Step 5: send_now_priority.**

```
send_now_priority =
    (round_fit_score * 2.0)         # gate variable; if cannot write the check, score doesn't matter
  + (lead_likelihood_score * 1.5)   # can they champion the deal
  + (composite_fit_score * 1.0)     # thesis fit
  + (cold_reachability_score * 0.5) # likelihood they reply at all
  + (signal_recency_bonus)          # 0 to 2 based on most_recent_signal_date (last 90 days = 2, 90-180 = 1, else 0)
  + (spiky_belief_score)            # 0 to 2 from axis variance
  - (kill_signal_penalty)           # -10 if any major kill signal
```

Critical: deterministic round_fit and lead_likelihood dominate the priority. The 4-axis composite contributes but does not control. This is the v2 reconciliation: thesis fit explains why a partner cares, deterministic fit decides whether they can act.

**Step 6: recommended_to_send.**

See the Recommended To Send section. Writes one row per partner to `partner_score_summaries`.

Done when: scores produced for all partners with at least one verified quality-≥2 signal, every partner has a row in `partner_score_summaries`, and the distribution of composite scores is non-degenerate.

### Stage 7: generate emails + write CSV

For top N partners by `send_now_priority` (default N=25), runs `prompts/generate_email.txt`. Validated against `schemas/email_generation.py`. Inserts into `email_drafts`.

The default of 25 matches the per-week send limit (Agent Build Rule 16) and realistic human review throughput. Reviewing 25 partners with 2 variants, deck response, follow-up, conversion hypothesis, and objection preemption is a 60-90 minute focused review session.

For each partner the system produces: two email variants using two DIFFERENT strategies (see Email Strategy section), a deck_request_response, a follow-up draft, and a conversion hypothesis + likely objection + preemption status.

After per-partner generation: batch-level QA (similarity check, template-smell, hard distribution gates). Any draft failing hard gates is regenerated using a different strategy or the partner is dropped.

Final step: writes `clients/{workspace}/exports/review_queue.csv` with the columns listed above.

Done when: each top-N partner has 2 strategy-differentiated email variants (or 1 with `limited_variation=true` documented), a deck_request_response, a follow-up draft, a conversion hypothesis, the batch satisfies hard gates, and the CSV is written.

### Stage 8: sync to Attio (optional)

Triggered only with `--with-attio` flag. Requires Stage 0 to pass first.

For each active fund:
- PUT `/v2/objects/companies/records` with matching attribute `domains` set to the fund's domain.
- Body includes all `fund_attributes` plus standard `name`, `domains`, `description`.
- Store returned `record_id` back into `funds.attio_record_id`.

For each scored partner:
- Local canonical key: `partner_id = slug(fund_domain + "_" + normalized_partner_name)`.
- Attio matching strategy, in order:
  1. If `email_addresses` known and verified: PUT with `email_addresses` matching attribute.
  2. If `linkedin_url` known: query by LinkedIn URL. If found, PATCH. If not, POST new.
  3. Fallback: query by `name` filtered by `company` link to the fund's Attio record. If single match, PATCH. If multiple, log conflict warning and skip (do not blindly create a duplicate).
- Body includes all `partner_attributes` plus standard `name`, `email_addresses` (if known), and the `company` link.
- Preserve-on-outreach-started logic: if existing record's `outreach_status` is in `preserve_on_outreach_started.statuses`, omit `preserved_fields`.
- Manual override protection: if `manual_score_override=TRUE`, omit score fields. If `manual_recommended_override=TRUE`, omit `recommended_to_send`.
- Store returned `record_id` back into `partners.attio_record_id`.
- All operations logged to `attio_sync_log`.

Done when: every partner with `recommended_to_send=TRUE` appears in Attio with `outreach_email_draft` populated and linked to the correct company record, and no duplicate person records exist for the same `partner_id`.

---

## Recommended To Send: calculation logic

A partner is set `recommended_to_send=TRUE` only when ALL of the following hold:

1. `composite_fit_score >= 6.5` (thesis fit threshold)
2. `round_fit_score >= 6.0` AND no `round_fit` disqualifier present (deterministic)
3. `lead_likelihood_score >= 5.0` OR `lead_likelihood_score IS NULL`
4. At least 2 distinct evidence sources verified at quality ≥2, satisfied by EITHER:
   - 2 verified thesis signals (quality ≥2) from 2 distinct source types (podcast, blog, essay, social, etc.), OR
   - 1 verified thesis signal (quality ≥2) plus 1 verified deal pattern (partner explicitly named in a stage-and-category-adjacent funding announcement, OR fund led the round and partner is listed as deal champion on fund site)
5. At least one verified quality-≥2 evidence item dated within the last 18 months
6. Partner's current fund employment is `verified_current` or `likely_current`
7. No major kill signal present
8. `cold_reachability_score >= 5` OR `cold_reachability_score IS NULL`
9. `warm_path_available != TRUE` (warm path takes precedence over cold)
10. At least one strategy in Stage 7 strategy eligibility scoring >= 2 (the partner has usable evidence for at least one specific email opener)

### Employment confidence levels

- `verified_current`: confirmed on fund's `/team` page AND on LinkedIn (or independent source) within the last 30 days
- `likely_current`: confirmed on one source only, or older than 30 days but no contradicting evidence
- `uncertain`: inferred from stale source or conflicting signals
- `left_fund`: explicit evidence of departure

For `recommended_to_send`, `verified_current` preferred but `likely_current` acceptable. `uncertain` and `left_fund` disqualify.

### Warm path override

If `warm_path_available=TRUE`, set `outreach_status="warm_path_needed"` instead of `ready_to_send`, regardless of score. Cold shots are precious; do not burn them on partners with higher-probability warm routes.

### Operational kill signals (hard)

A single major kill signal makes the partner not recommended regardless of other scores:

- Only invests post-revenue Series A+ when company raising seed (stage mismatch)
- Explicitly avoids user's category
- Fund has not deployed in last 18 months
- Partner has left the fund
- Public anti-cold-outreach stance
- Check size constraint outside the round's range

### Soft kill signals (warning, not auto-disqualification)

- Follow-on-only pattern in last 3 visible deals (could be fund-cycle artifact)
- No partner-attributed lead in last 12 months (may just mean attribution gap)
- Sparse public footprint generally

For each soft kill triggered, write to `kill_signal_summary` with "soft:" prefix so the user sees it during review.

If any criterion fails, the partner is still scored and stored, but `recommended_to_send` is `FALSE`. The user can override by editing the flag (and setting `manual_recommended_override=TRUE`).

---

## Email Strategy and Batch QA

### Six email strategies

Stage 7 selects one of six strategies per email variant. The strategy determines the opening move and the sentence structure. Every strategy ends with the same direct pitch-meeting ask.

1. **signal_led**: opens with a sharp recent quote or stated position.
2. **portfolio_led**: opens with an adjacent portfolio investment.
3. **round_pattern_led**: opens with a comparable recent round they led.
4. **market_shift_led**: opens with category timing.
5. **contrarian_thesis_led**: opens by extending or pressuring a stated belief.
6. **traction_led**: opens with the company's momentum.

The two variants per partner must use TWO DIFFERENT strategies (real structural variation, not just two angles of the same strategy).

### Strategy eligibility scoring (before selection)

Before selecting strategies, Stage 7 scores each of the 6 strategies for the partner 0-3:

- 0 = unsupported (no evidence; do not use)
- 1 = weakly supported (some evidence but thin)
- 2 = supported (evidence concrete enough to ground an opener)
- 3 = strongly supported (evidence direct, recent, specific)

A strategy may only be used if eligibility >= 2. The signal driving a `signal_led` opener must have `signal_quality_score >= 3`.

If only one strategy scores >= 2, generate one variant and set `limited_variation=true` per the schema. If zero strategies score >= 2, drop the partner from the recommended set entirely and log the reason.

Evidence quality always wins over distribution diversity.

### Conversion hypothesis (required per recommended variant)

Before generating, the model articulates the conversion hypothesis: a one-sentence statement of WHY this specific partner is likely to book a meeting for this raise based on available evidence. Not a probability. A reasoning statement.

If the model cannot generate a coherent hypothesis, the email should not be generated for that variant.

### Likely objection + preemption (required per recommended variant)

For the recommended variant:

- Identify the single most likely objection this partner will use to dismiss the email (e.g., "too early," "wedge too narrow," "wrong category," "stage mismatch")
- Decide if the objection is disqualifying (no email can fix it; the partner should not be in the batch) or preemptable
- If preemptable, the email body should contain one sentence that quietly addresses it. Identify it in `preemption_line` and set `objection_preempted=true`
- If not preemptable, set `objection_preempted=false`. The user sees this in the CSV/Attio and may choose to drop the partner

### Similarity check (within batch)

After all per-partner generation completes, compute pairwise similarity across recommended drafts using `rapidfuzz.fuzz.token_set_ratio`. Normalize 0-100 scores to 0.0-1.0 (divide by 100). Flag pairs where:

- Subject similarity > 0.75
- First sentence similarity > 0.70
- Full body similarity > 0.82

Flagged drafts must be regenerated using a DIFFERENT strategy. If no alternate strategy is viable, drop the partner.

### Template smell validator (LLM judge)

After per-partner generation, run a second LLM pass on each recommended variant. The judge sees the current draft plus the 5 MOST SIMILAR existing recommended drafts in the batch (by body similarity from the previous step).

Output:

```json
{
  "template_smell": "low | medium | high",
  "sounds_mass_generated": true | false,
  "too_similar_to_neighbors": true | false,
  "reason": "one sentence",
  "required_fix": "change strategy | change opener | change CTA framing | rewrite in founder voice | no fix needed"
}
```

Any draft with `template_smell=high` or `sounds_mass_generated=true` is regenerated.

### Batch distribution checks

**Hard gates** (drafts violating these must be regenerated or dropped):

- Any pair of drafts with body similarity above 0.82
- Any draft with `template_smell=high`
- Any draft missing explicit raise reference in the body
- Any draft with soft CTA ("thesis chat", "feedback", "pressure-test", "compare notes")
- Any draft built from an unsupported strategy (eligibility < 2)

**Warning gates** (surface to user, do NOT auto-regenerate):

- More than 35 percent of recommended emails use the same primary strategy
- More than 25 percent share the same first-sentence structural pattern
- More than 20 percent use identical CTA wording
- Less than 80 percent have `template_smell=low`

Strategy distribution never overrides evidence quality. If most strong emails are signal_led, the batch may remain signal_led as long as individual emails are specific, non-duplicative, and conversion-oriented.

### Follow-up draft (one per partner)

Stage 7 also generates a single follow-up draft per partner, stored in `followup_email_draft`. Requirements:

- 2 sentences maximum
- Sent 4-6 business days after no response (user-triggered, never automatic)
- Must NOT repeat the same personalization signal from the first email unless reframed sharply
- Must add one new piece of context: new traction, raise milestone, time pressure, different partner-specific angle
- Must ask for the pitch meeting again, not soften the ask

This is a FALLBACK TEMPLATE, not a final draft. Before sending, the user should update with any new raise momentum.

### Deck request response (one per partner)

A 2-3 sentence reply for when the partner asks "just send the deck." Sends the deck AND steers to a meeting. Does not imply the deck is withheld. Frames the call as a way to make the deck more useful, not as a condition to receive it.

---

## Background jobs

### `jobs/attio_outcome_sync.py`

Runs daily (or on demand). Pulls partner records modified in the last 7 days from Attio via `POST /v2/objects/people/records/query` filtered by `last_modified`. For each, reads `outreach_status`, `reply_type`, `meeting_booked`, `meeting_date`, `meeting_outcome`. Upserts into `outcomes` table.

### `jobs/monthly_learning_report.py`

Runs monthly. Reads workspace `outcomes` table. Produces a learning report, not an optimizer. Cold outreach volume during a single raise is too low and biased for a real model.

For each axis and each `reply_type` outcome, the report computes:

- Mean score among partners who booked a meeting
- Mean score among partners who did not book
- Sample size in each group
- Pattern flags

Additionally tracks calibration data for assumptions the spec deliberately did not bake in as weights:

- Reply rate and meeting rate bucketed by `cold_reachability_score`
- Reply rate by `email_strategy_used`
- Reply rate by `axis_score_variance` bucket (does spikiness actually outconvert?)

Writes weight-change SUGGESTIONS to `axis_weight_suggestions`. The job NEVER modifies `config/axes.yaml` directly.

Confidence levels:

- `low` if combined sample size under 30
- `medium` if 30 to 100
- `high` if above 100

If cross-workspace learning is opted in, also writes anonymized aggregate stats to `core/cross_workspace_stats.json` (no partner names, no email text, no fund names — only strategy/timing/bucket → rate mappings).

### `jobs/apply_axis_suggestion.py`

Applies a specific suggestion by ID to `clients/{workspace}/config/axes.yaml` after user review. Backs up the previous axes.yaml before applying.

---

## Pydantic schemas

### `schemas/fund_enrichment.py`

```python
from pydantic import BaseModel, HttpUrl
from typing import Optional, List

class Partner(BaseModel):
    name: str
    title: Optional[str] = None
    bio_snippet: Optional[str] = None

class FundEnrichment(BaseModel):
    thesis_summary: Optional[str] = None
    stated_sectors: List[str] = []
    stated_stage_focus: Optional[str] = None
    check_size_range: Optional[str] = None
    portfolio_companies: List[str] = []
    current_partners: List[Partner] = []
    recent_focus_signals: Optional[str] = None
    explicit_kill_signals: List[str] = []
    source_urls_used: List[HttpUrl] = []
```

### `schemas/partner_signals.py`

```python
from pydantic import BaseModel, HttpUrl
from typing import List, Optional
from datetime import date

class Signal(BaseModel):
    quoted_text: str
    source_url: HttpUrl
    source_type: str  # podcast, blog, essay, social, fund_site, funding_announcement, interview
    quote_date: Optional[date] = None
    axis_relevance: List[str]  # must be non-empty
    signal_direction: str  # "positive" or "negative"
    confidence: str  # "high", "medium", "low"

class EvidenceSignal(BaseModel):
    """Used for reachability evidence."""
    evidence: str
    source_url: HttpUrl
    direction: str

class PartnerSignalsOutput(BaseModel):
    signals: List[Signal]
    reachability_signals: List[EvidenceSignal]
    cold_reachability_partial_score: Optional[float] = None
    cold_reachability_reasoning: Optional[str] = None
```

Note: `round_fit_*` and `lead_likelihood_*` are NOT in this schema. They are deterministic and computed in Stage 6.

### `schemas/signal_quality.py`

```python
from pydantic import BaseModel
from typing import Literal

class SignalQuality(BaseModel):
    signal_quality_score: Literal[0, 1, 2, 3]
    quality_reasoning: str
```

### `schemas/candidate_score.py`

```python
from pydantic import BaseModel
from typing import List, Optional, Dict

class AxisScore(BaseModel):
    score: Optional[float] = None  # 0 to 10, or None if insufficient data
    supporting_signal_ids: List[int] = []
    confidence: str  # "low", "medium", "high"
    reasoning: str

class CandidateScore(BaseModel):
    axis_scores: Dict[str, AxisScore]  # keyed by axis_id
```

### `schemas/deal_attribution.py`

```python
from pydantic import BaseModel
from typing import List, Optional
from datetime import date

class AttributedPartner(BaseModel):
    name: str
    fund: str

class DealAttribution(BaseModel):
    company: str
    round_type: str
    round_size_usd: Optional[int] = None
    lead_investor: Optional[str] = None
    all_investors: List[str] = []
    attributed_partners: List[AttributedPartner] = []
    sector_tags: List[str] = []
    announcement_date: Optional[date] = None
```

### `schemas/email_generation.py`

```python
from pydantic import BaseModel, Field, validator
from typing import List, Literal, Optional

Strategy = Literal[
    "signal_led",
    "portfolio_led",
    "round_pattern_led",
    "market_shift_led",
    "contrarian_thesis_led",
    "traction_led",
]

class EmailVariant(BaseModel):
    strategy: Strategy
    subject: str = Field(..., max_length=80)
    body: str
    conversion_hypothesis: str
    likely_objection: str
    objection_preempted: bool
    preemption_line: Optional[str] = None
    template_smell: str = "unscored"

class EmailOutput(BaseModel):
    variants: List[EmailVariant]
    recommended_variant_strategy: Strategy
    recommendation_reasoning: str
    limited_variation: bool = False
    limited_variation_reason: Optional[str] = None
    deck_request_response: str
    followup_draft: str

    @validator('variants')
    def variants_count_and_uniqueness(cls, v):
        if len(v) not in (1, 2):
            raise ValueError("Must produce 1 or 2 variants")
        if len(v) == 2 and v[0].strategy == v[1].strategy:
            raise ValueError("If producing 2 variants, they must use different strategies")
        return v

    @validator('limited_variation_reason', always=True)
    def reason_required_if_limited(cls, v, values):
        if values.get('limited_variation') and not v:
            raise ValueError("limited_variation_reason required when limited_variation=True")
        return v
```

---

## Prompts

### `prompts/enrich_fund.txt`

```
You are extracting structured data about a venture capital fund from fetched web pages.

Fund: {FUND_NAME}
Domain: {DOMAIN}

Below is content fetched from this fund's website. Extract the listed fields. Use only information present in the content. If a field is not present, return null. Do not infer from training data. Do not invent URLs.

Return JSON matching this schema:
{
  "thesis_summary": "one sentence in the fund's own language, or null",
  "stated_sectors": [list],
  "stated_stage_focus": "pre-seed, seed, series A, multi-stage, or null",
  "check_size_range": "as stated, or null",
  "portfolio_companies": [list of company names from portfolio pages],
  "current_partners": [{"name": "...", "title": "...", "bio_snippet": "..."}],
  "recent_focus_signals": "thesis updates from last 90 days, or null",
  "explicit_kill_signals": [list of strings],
  "source_urls_used": [URLs you drew from]
}

FETCHED CONTENT:
{CONTENT}
```

### `prompts/extract_partner_signals.txt`

```
You are extracting two types of evidence about a specific VC partner from content they authored or that features them. The company {COMPANY_NAME} is actively raising a {ROUND} of {AMOUNT}, so the goal is to assess thesis fit and cold reachability.

Partner: {PARTNER_NAME}
Fund: {FUND_NAME}

Target axes for {COMPANY_NAME}:
{AXES_BLOCK}

PART 1: THESIS SIGNALS

For each axis above, search the content for statements by this partner that signal fit or anti-fit on that specific axis.

Strict requirements:
- Quote the partner's exact words. Do not paraphrase.
- If you cannot quote exactly, skip the signal.
- Provide the exact source URL where the quote appears.
- Provide the date if available.
- Mark which axis or axes the signal speaks to.

Signals do not need to use the company's exact vocabulary. Score for adjacent beliefs, investment behavior, and repeated reasoning patterns, not keyword overlap.

If no thesis signals found, return signals=[].

PART 2: COLD REACHABILITY SIGNALS

Independent of thesis fit. Does this partner take cold inbound? Positive: public writing/podcast in last 12 months, explicit cold-inbound statements, deals visibly sourced from cold. Negative: explicit anti-cold statements, board-heavy late-stage with no recent leads, follow-on-only, no public presence in 12 months.

Produce `cold_reachability_partial_score` 0 to 10 with one-sentence reasoning. (The final reachability score will be computed in Stage 6 by combining this with deterministic checks.)

DO NOT score round_fit or lead_likelihood. Those are deterministic and computed elsewhere.

Return JSON matching the PartnerSignalsOutput schema. Do not invent evidence.

CONTENT:
{CONTENT}
```

### `prompts/signal_quality.txt`

```
You are rating the quality of a verified investor signal on a 0-3 scale.

The signal has already been verified for provenance (the quote is real and the URL resolves). Your job is to rate its USEFULNESS for predicting investor fit for {COMPANY_NAME}.

Company description:
{COMPANY_DESCRIPTION}

Signal:
Quote: "{QUOTED_TEXT}"
Source: {SOURCE_URL}
Tagged axis: {AXIS_RELEVANCE}
Date: {QUOTE_DATE}

Scale:
0 = verified but meaningless (generic affirmation like "yeah, X is interesting" that says nothing predictive)
1 = generic category mention (referenced the category but not in a way that signals belief or behavior)
2 = relevant but broad (expressed a view on the category that suggests fit but does not specifically map to the company's wedge)
3 = specific, recent, and predictive of investor fit for this raise

Calibration examples (anchor your rating to these):
{CALIBRATION_EXAMPLES}

Return JSON:
{
  "signal_quality_score": 0|1|2|3,
  "quality_reasoning": "one sentence"
}
```

### `prompts/score_candidate.txt`

```
You are scoring a VC partner's THESIS AND PERSONALITY fit for {COMPANY_NAME} against {N_AXES} axes.

You are NOT scoring round eligibility, check size match, lead capability, or recency of deployment. Those are computed deterministically and provided to you for context only.

Company description:
{COMPANY_DESCRIPTION}

Axes (measure investor psychology and belief, not sector):
{AXES_BLOCK}

Partner bio:
{PARTNER_BIO}

Fund thesis:
{FUND_THESIS}

All verified, quality-≥2 signals from this partner:
{SIGNALS_JSON}

Deterministic round_fit_score (for context): {ROUND_FIT_SCORE}
Deterministic lead_likelihood_score (for context): {LEAD_LIKELIHOOD_SCORE}

For each axis, return:
- score (0 to 10) or null if insufficient data
- supporting_signal_ids (list of integer IDs)
- confidence ("low", "medium", "high") based on signal volume and recency
- reasoning (one sentence)

Use only provided data. Do not infer. Do not use round_fit or lead_likelihood to inflate axis scores.

Return JSON: {"axis_scores": {"axis_1": {...}, "axis_2": {...}, ...}}
```

### `prompts/attribute_deal.txt`

```
Extract structured deal data from this funding announcement.

Required JSON output:
{
  "company": "name",
  "round_type": "seed, series A, etc",
  "round_size_usd": integer or null,
  "lead_investor": "fund name or null",
  "all_investors": [list of fund names],
  "attributed_partners": [{"name": "...", "fund": "..."}],
  "sector_tags": [list],
  "announcement_date": "YYYY-MM-DD"
}

Only attribute partners when the announcement explicitly names them as leading or championing. Use only what is in the announcement.

ANNOUNCEMENT:
{ANNOUNCEMENT_TEXT}
```

### `prompts/generate_email.txt`

```
You are drafting a cold outreach email from {FOUNDER_NAME}, founder of {COMPANY_NAME}, to {PARTNER_NAME} at {FUND_NAME}.

CRITICAL CONTEXT: {COMPANY_NAME} is actively raising a {ROUND} of {RAISE_AMOUNT}. This email exists solely to convert into a pitch meeting for this raise. It is NOT a networking email, thesis discussion, feedback request, wedge review, pressure-test, or general update. Every sentence must serve the goal of getting {PARTNER_NAME} to book {MEETING_DURATION} minutes via {MEETING_FORMAT} to hear the pitch for this specific round.

Raise context:
- Round: {ROUND}
- Amount: {RAISE_AMOUNT}
- Status: {RAISE_STATUS}
- Timing: {RAISE_TIMING}
- Why this round is fundable now: {WHY_THIS_ROUND_IS_FUNDABLE_NOW}
- What changes after this round: {WHAT_CHANGES_AFTER_THIS_ROUND}

Round hook (use the strongest piece in sentence 2, 3, or 4):
- Strongest reason to meet now: {ROUND_HOOK_REASON}
- Investor consequence of waiting: {ROUND_HOOK_CONSEQUENCE}
- Round momentum proof: {ROUND_HOOK_MOMENTUM_PROOF}

Company context:
{COMPANY_DESCRIPTION}

Available proofs (use the one most likely to convert THIS partner; do not blindly use headline traction):
- Founder-designated strongest proof: {STRONGEST_RAISE_PROOF}. Default to this unless this partner's signals strongly suggest a different proof would land better.
- Headline traction: {HEADLINE_METRIC}
- Secondary metrics: {SECONDARY_METRICS}
- Customer/pilot/LOI evidence: {CUSTOMER_EVIDENCE}
- Technical validation: {TECHNICAL_VALIDATION}
- Non-dilutive funding or strategic anchors: {NON_DILUTIVE_OR_STRATEGIC}
- Founder-market fit: {FOUNDER_MARKET_FIT}

Research about this partner:
- Composite fit score: {COMPOSITE_SCORE} of 10 (thesis/personality only)
- Round fit score: {ROUND_FIT_SCORE} of 10 (deterministic)
- Lead likelihood score: {LEAD_LIKELIHOOD_SCORE} of 10 (deterministic)
- Top-scoring axes: {TOP_AXES_NAMES_AND_SCORES}
- Top 3 verified, quality-≥2 signals (with sources and dates): {TOP_SIGNALS}
- Available portfolio adjacencies: {ADJACENT_PORTFOLIO_COMPANIES}
- Recent rounds led by this partner: {RECENT_PARTNER_LED_DEALS}
- Communication style observed: {COMM_STYLE}
- Kill signals to avoid triggering: {KILL_SIGNALS}

Founder voice:
- Style: {FOUNDER_VOICE_STYLE}
- Banned phrases: {FOUNDER_BANNED_PHRASES}

STEP 1: SELECT STRATEGIES

Score each of the 6 strategies for this partner 0-3:
- signal_led: requires a quality-3 recent quote. Examples in {EXAMPLES_DIR}/signal_led.md
- portfolio_led: requires an adjacent portfolio company. Examples in {EXAMPLES_DIR}/portfolio_led.md
- round_pattern_led: requires a recent partner-led round in adjacent space. Examples in {EXAMPLES_DIR}/round_pattern_led.md
- market_shift_led: requires partner activity in category without strong quote. Examples in {EXAMPLES_DIR}/market_shift_led.md
- contrarian_thesis_led: requires a strong public thesis to extend or pressure. Examples in {EXAMPLES_DIR}/contrarian_thesis_led.md
- traction_led: requires strong company traction AND a metrics-oriented partner signal. Examples in {EXAMPLES_DIR}/traction_led.md

Selection rules (in priority order):
1. Evidence quality always wins over variety. Do not pick a strategy that lacks supporting evidence just to satisfy the two-variant requirement.
2. If two strategies score >= 2, pick the two best fits. They must be genuinely different in opening logic; do not pick signal_led twice with different signals.
3. If only one strategy scores >= 2, generate ONLY ONE variant and set limited_variation=true with a one-sentence reason.
4. If zero strategies score >= 2, return an empty variants list and set limited_variation=true with reason. This partner should be dropped from the recommended set.

STEP 2: GENERATE EACH VARIANT

For each chosen strategy, load the corresponding example file as a style anchor. Match its directness, density, and voice. Do not copy wording.

Critical framing: the investor signal is a DOORWAY, not the argument. Do not make the email about the partner's quote or portfolio. Make the email about why the company and round are relevant to the partner now. The signal opens the door; the round hook and proof carry the meeting ask.

Body: 4 sentences maximum. One personalization signal only.

Sentence 1 (varies by strategy): the opener pattern from the strategy description. No flattery. No "I noticed you", "I came across your", "I've been following".

Sentence 2: one-line company description connecting to sentence 1. Include the proof most likely to convert THIS partner.

Sentence 3: ONE of the following, whichever fits better:
- One concrete reason this partner specifically is the right person for this raise, OR
- The round hook (strongest reason to meet now plus round momentum proof)

Sentence 4: the pitch ask. The email must contain: (a) "raising" or "raise" making the active raise unambiguous, (b) the meeting duration, (c) at least one of: scheduling link, specific time slots, or both.

CTA format options:
- Link-only: "...book {MEETING_DURATION} minutes to walk you through the company and round: {SCHEDULING_LINK}"
- Slots-only: "...I can walk you through the company and round Tuesday at {TIME_1} or Wednesday at {TIME_2}."
- Hybrid (preferred for high-priority partners): "...I'd like to book {MEETING_DURATION} minutes. I'm open Tuesday {TIME_1} or Wednesday {TIME_2}, or here is my link: {SCHEDULING_LINK}."

Never "would love to chat", "grab coffee", "compare notes", "pressure-test", "thesis discussion", "feedback session".

Subject line: 5 words maximum. Not a question. Specific.

Forbidden phrases: "building the future of", "would love", "circling back", "wanted to reach out", "hope this finds you well", "quick question", "pressure-test", "compare notes", "thesis chat", "get your feedback", plus anything in {FOUNDER_BANNED_PHRASES}. No em dashes. No exclamation marks.

STEP 3: GENERATE CONVERSION HYPOTHESIS

For the recommended variant, state in one sentence WHY this partner is likely to book based on the evidence. Not a probability. A reasoning statement.

STEP 4: IDENTIFY AND PREEMPT THE LIKELY OBJECTION

For the recommended variant:
- Identify the single most likely objection this partner will use to dismiss the email
- Decide if the objection is disqualifying (no email can fix it; the partner should not be in the batch) or preemptable
- If preemptable, the email body should contain one sentence that quietly addresses it. Identify it in preemption_line and set objection_preempted=true
- If not preemptable, set objection_preempted=false

STEP 5: GENERATE DECK REQUEST RESPONSE

A 2 to 3 sentence reply for when the partner asks "just send the deck." Send the deck AND steer to a meeting. Do not imply the deck is withheld. Frame the call as a way to make the deck more useful.

STEP 6: GENERATE FOLLOW-UP DRAFT (fallback template)

A single 2-sentence follow-up for 4-6 business days after no response. Before sending, the user should update with new raise momentum. Must add ONE NEW piece of context, must not repeat the first email's signal, must still ask for the pitch meeting.

Return JSON matching the EmailOutput schema.
```

---

## Acceptance criteria

The system is complete when:

1. The workspace-aware config loader runs and routes to the correct workspace directory.
2. Session 1's vertical slice produces one valid CSV row for a fixture partner.
3. `scripts/00_*` succeeds against a real Attio workspace when configured.
4. The full fixture run (Gates 1-5) produces a CSV review queue with 5 partners, each containing two strategy-differentiated email variants, deck_request_response, follow-up draft, conversion hypothesis, likely objection, preemption status. Optional Attio sync produces matching records.
5. The verification rate (verified signals over total signals) is between 50 and 80 percent on the fixture, AND at least 10 signals have `signal_quality_score >= 2`.
6. Each email draft is a 4-sentence body opening with a quality-≥3 verified-quote signal (when signal_led), including the proof most relevant to that partner, explicitly mentioning the active raise, ending with a concrete pitch-meeting ask containing the user's scheduling link.
7. `round_fit_score` and `lead_likelihood_score` are computed deterministically with no LLM inference; the LLM may only produce one-line reasoning text describing the deterministic result.
8. `jobs/attio_outcome_sync.py` pulls outreach status from Attio into the workspace `outcomes` table without errors.
9. `jobs/monthly_learning_report.py` produces rows in `axis_weight_suggestions` without modifying `config/axes.yaml` directly.
10. Re-running any stage on the fixture produces no duplicate records.
11. Records in Attio with `outreach_status` of `sent` or beyond are not overwritten by a re-sync.
12. Records with `manual_score_override=TRUE` or `manual_recommended_override=TRUE` are not overwritten by routine syncs. `--force-rescore --reason "..."` is required and logged.
13. Every LLM output that fails schema validation is retried up to 3 times, then logged and skipped.
14. The `runs` table contains one row per script execution with accurate processed/succeeded/failed/skipped counts.
15. The system never sends emails directly, never bulk-exports a send queue beyond 25 records per day, and never marks more than 25 records per day as `ready_to_send` without explicit user approval logged in the `runs` table.
16. Cross-workspace learning is OFF by default. When opted in, no partner names, fund names, email text, or signals leak between workspaces — only anonymized aggregate stats.
17. Process-wide rate limiter for the Anthropic API is shared across concurrent workspace runs; no 429s from internal contention.

---

## Calibration Outcomes (Gate 5.5)

After the calibration batch is sent and ≥5 business days have passed:

- **Green**: 2+ meetings booked from 8-10 sends, OR 1 meeting plus 2+ substantive replies from relevant partners
- **Yellow**: 1 meeting booked, OR 2+ substantive replies without a meeting
- **Red**: no meaningful replies, generic passes, or replies indicating wrong stage/category

If Green: proceed to top 25.
If Yellow: revise prompts/examples/company.yaml once, run one more calibration batch on different mid-tier partners.
If Red: do not scale. Iterate on examples, round_hook, strongest_raise_proof, or the email prompt. Re-run calibration on a new batch.
If Red twice: stop using cold pipeline for this raise beyond manual one-offs. Bottleneck is upstream of email mechanics.

---

## Ship Condition

After these boundary fixes are applied, do not add new scoring systems, new generated artifacts, or new workflow stages before the first vertical slice is built.

The next milestone is a CSV-first vertical slice that produces one usable outreach row end to end.

Specifically:

1. No new strategies beyond the 6 listed.
2. No new scoring axes beyond the 4 from `axes.yaml`.
3. No new background jobs beyond outcome sync and monthly learning report.
4. No new Attio attributes beyond those listed.
5. No new CSV columns beyond those listed.
6. No new schemas beyond the 6 listed.

If the build encounters a missing capability that requires expanding the spec, stop and request explicit approval before adding. Scope creep at the spec level invalidates the timebox.

---

## Things to ask before starting

1. Anthropic API key configured in repo-root `.env`?
2. Attio API key and workspace ID configured in `clients/{workspace}/.env`? (Only required if using Attio path; CSV path runs without it.)
3. Workspace name confirmed (e.g., `clients/oko_seed`)?
4. Have the custom Attio attributes been created per the Attio Setup section? (Only required if using Attio path.)
5. Has the user exported an OpenVC investor list to `clients/{workspace}/data/raw/openvc_export.csv`?
6. Listen Notes API key for podcast mining? (Optional.)
7. Are `clients/{workspace}/config/company.yaml` and `clients/{workspace}/config/axes.yaml` populated, and have the axes been confirmed orthogonal?
8. Is `raise_context` populated in `company.yaml`? This system only operates when an active raise is in progress.
9. Is `clients/{workspace}/prompts/examples/` populated with the minimum example set: 3 signal_led, 3 portfolio_led or market_shift_led, 2 follow_up, 2 deck_request_response?
10. Is `core/calibration/signal_quality_examples.json` populated with company-agnostic 0-3 examples? (Shared across workspaces; written once.)
11. Confirmed build timebox: 8 hours to first vertical slice, 12 hours max before reassessing?

---

## What this spec deliberately omits

- Email sending: drafts go to CSV and optional Attio; user sends via their normal workflow.
- Warm path detection: handled outside this system.
- Paid data sources: free-only in v1.
- Multi-user authentication: single founder per workspace; tenancy is filesystem-level.
- Standalone web frontend: CSV and Attio are the frontend.
- Voice extraction from sent emails: v2 only. v1 requires hand-written examples.
- Cross-workspace fit learning: only anonymized operational stats cross workspaces, and only opt-in.

The success metric is meetings booked from emails this system drafted for the active raise.
