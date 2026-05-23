# Investor Outreach Pipeline

Workspace-based VC partner outreach pipeline. Builds a verified, scored target
list of VC partners, drafts pitch-meeting emails per partner, and produces a
reviewable CSV (plus optional Attio sync). Built per `PROJECT_BRIEF.md`.

## Quick start (5 commands)

```bash
uv sync
cp .env.example .env && $EDITOR .env                  # add ANTHROPIC_API_KEY
uv run scripts/init_workspace.py my_raise              # scaffold clients/my_raise/
$EDITOR clients/my_raise/config/*.yaml \
        clients/my_raise/prompts/examples/*.md         # fill the templates
export INVESTOR_WORKSPACE=clients/my_raise             # so --workspace is optional
# then run the pipeline:
uv run scripts/01_aggregate_sources.py
uv run scripts/02_enrich_funds.py
uv run scripts/03_mine_activity.py
uv run scripts/04_mine_partner_signals.py
uv run scripts/05_verify_and_quality.py
uv run scripts/06_score_candidates.py
uv run scripts/07_generate_emails.py --top 25
# review clients/my_raise/exports/review_queue.csv
uv run scripts/status.py                               # any time, to see state
```

## Setup

```bash
uv sync
cp .env.example .env   # each operator supplies their own ANTHROPIC_API_KEY
```

Per-workspace state lives under `clients/{workspace}/`. Code under `core/` and
`scripts/` is tenant-agnostic. Every script takes `--workspace` or falls back
to `INVESTOR_WORKSPACE` env var.

## End-to-end run (test fixture)

```bash
uv run scripts/01_aggregate_sources.py    --workspace clients/test_workspace
uv run scripts/02_enrich_funds.py         --workspace clients/test_workspace --fixtures
uv run scripts/03_mine_activity.py        --workspace clients/test_workspace --fixtures
uv run scripts/04_mine_partner_signals.py --workspace clients/test_workspace --fixtures
uv run scripts/05_verify_and_quality.py   --workspace clients/test_workspace
uv run scripts/06_score_candidates.py     --workspace clients/test_workspace
uv run scripts/07_generate_emails.py      --workspace clients/test_workspace --top 5
# -> clients/test_workspace/exports/review_queue.csv
```

Optional Attio sync (requires `clients/{workspace}/config/attio.yaml` and
`ATTIO_API_KEY` in `clients/{workspace}/.env`):

```bash
uv run scripts/00_verify_attio_schema.py --workspace clients/{name}
uv run scripts/08_sync_to_attio.py       --workspace clients/{name}
```

## Tests

```bash
uv run pytest tests/ -v
```

End-to-end smoke runs all stages on a temp copy of `test_workspace` and asserts
row counts, recommended count, CSV shape, sector_tags persistence, and
idempotency. Also covers the ready_to_send ceiling refusal and the manual
override preservation paths. CI runs on every push and PR (no API key needed —
LLM stays in stub mode).

## Operator controls

These flags exist because the brief mandates explicit human approval for
specific high-blast-radius actions:

- `--force-rescore --reason "..."` (Stage 6): bypass `manual_score_override` /
  `manual_recommended_override` on a per-partner basis. Without it, routine
  Stage 6 runs skip overridden partners. Every changed field is logged to
  `force_refresh_log`.
- `--approve-bulk-ready --reason "..."` (Stage 7): allow more than 25 partners
  to be marked `ready_to_send` in a single run (Brief Rule 16 hard ceiling).
  Approval is persisted as a note on the `runs` row.

## Operator CLIs (no SQL required)

- `scripts/init_workspace.py NAME` — scaffold a new `clients/NAME/` with template
  config + example stubs.
- `scripts/status.py` — single-pane view: counts per stage, last-run timestamps,
  pending suggestions, recent errors, suggested next command.
- `scripts/manual_override.py --partner-id X --score|--recommended|--warm-path
  --reason "..."` — flip override flags without raw SQL. `--list` shows what's
  set; `--clear` removes everything for a partner.
- `scripts/record_outcome.py --partner-id X --status STATUS --reply-type TYPE
  [--meeting-booked ...]` — append outcomes for the monthly learning report
  without going through Attio. `--from-csv path.csv` does batch import.
- `jobs/apply_axis_suggestion.py --list | --suggestion-id N | --all-above LEVEL`
  — review and apply axis-weight suggestions. Keeps the 10 most-recent
  `axes.yaml.bak.*` backups, rotates older ones.

## Known limitations

- **Employment classification.** Stage 2's team-page discovery only produces
  `likely_current`. The other states (`verified_current`, `uncertain`,
  `left_fund`) require additional sources (LinkedIn cross-check, a departures
  feed) that this v1 does not ingest. Partners who actually left a fund will
  appear in the pipeline until manually flagged.
- **Cold-inbound contact info on fund site.** Brief Stage 6 Step 4 lists this
  as one of the deterministic components of `cold_reachability_score`. Stage 2
  enrichment does not yet extract contact-info presence; it is treated as 0.
- **Stub-mode email generation.** When no `ANTHROPIC_API_KEY` is set, Stage 7
  uses an in-script `EMAIL_BANK` instead of calling the LLM. The bank only
  covers the five fixture partners; any other partner in stub mode triggers a
  WARN and gets zero variants (dropped from CSV). Real workspaces with an API
  key never hit this path.
- **Live network paths.** Stage 2's fund scraping and Stage 4's content
  ingestion are built but unexercised in this build (fixture mode supplies
  local HTML/text). First real run should use a throwaway target list and
  validate the verification rate falls in the brief's 50-80% band.
- **Background jobs (Session 9).** Attio outcome sync, monthly learning
  report, and axis-weight-suggestion applier are not yet built.

## Build status

Sessions 1-8 complete. PR: see `claude/relaxed-heisenberg-oEg7W`.
