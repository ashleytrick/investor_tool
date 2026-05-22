# Investor Outreach Pipeline

Workspace-based VC partner outreach pipeline. Builds a verified, scored target
list of VC partners, drafts pitch-meeting emails per partner, and produces a
reviewable CSV (plus optional Attio sync). Built per `PROJECT_BRIEF.md`.

## Setup

```bash
uv sync
cp .env.example .env   # each operator supplies their own ANTHROPIC_API_KEY
```

Per-workspace state lives under `clients/{workspace}/`. Code under `core/` and
`scripts/` is tenant-agnostic. Every script takes `--workspace`.

## Build status

- **Session 1 (vertical slice, CSV-first): built.** Produces one CSV row end to
  end for a fixture partner. LLM runs in stub mode when no `ANTHROPIC_API_KEY`
  is resolvable.

```bash
uv run scripts/07_generate_emails.py --workspace clients/test_workspace
# -> clients/test_workspace/exports/review_queue.csv
```

Sessions 2-11 thicken each pipeline stage; see `PROJECT_BRIEF.md`.
