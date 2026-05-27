# Scheduler / cron-hook wiring

The API ships 10 cron-style hook endpoints under
`POST /api/public/hooks/...`. They do nothing until something
calls them on a schedule. `.github/workflows/scheduler.yml`
does that via GitHub Actions scheduled workflows.

## What this unblocks

10 features in the catalog that are flagged ⚠️ "needs more"
because of missing cron:

| Hook | Cadence | Unblocks |
|---|---|---|
| `poll-gmail-sent` | every 10 min | `/sent` tab populates; touch-1 send events drive follow-up gating + send-pace cap |
| `poll-gmail-replies` | every 10 min | `/replies` tab populates; reply-classifier runs; auto-stop on reply fires |
| `reconcile-drafts` | every 30 min | Sequence auto-stop on reply (per-tenant fan-out) |
| `build-follow-ups` | daily @ 06:00 UTC | FR-5 follow-up draft generation |
| `poll-crm-activity` | every 15 min | Attio notes / tasks → `outreach_events` |
| `poll-crm-pipeline` | every 30 min | CRM stage-advance auto-stop (waits on Attio schema mapping too) |
| `poll-crm-investors` | every 6 h | Bulk investor pull from Attio (waits on schema mapping) |
| `poll-crm-relationships` | every 6 h | Relationship events from Attio (waits on schema mapping) |
| `poll-crm-lists` | every 1 h | Attio list-membership snapshot (waits on schema mapping) |
| `poll-crm-deals` | every 1 h | Attio deals (waits on schema mapping) |

## One-time setup

You need two GitHub repo secrets. They're consumed by
`.github/workflows/scheduler.yml`:

1. **`HOOK_BASE_URL`** — the API's public base, e.g.
   `https://investor-outreach-api-trick.fly.dev`. No trailing slash.

2. **`HOOK_SECRET`** — must match the `HOOK_SECRET` env var on
   the Fly app. The hook endpoint compares with `hmac.compare_digest`
   and 401s on mismatch.

### Set them via the `gh` CLI

```bash
gh secret set HOOK_BASE_URL --body "https://investor-outreach-api-trick.fly.dev"

# Use whatever value you set on Fly for HOOK_SECRET:
gh secret set HOOK_SECRET --body "<your-hook-secret>"
```

### Or via the GitHub web UI

Repo → Settings → Secrets and variables → Actions → New repository secret.

### Set the matching Fly secret

If you haven't already:

```bash
fly secrets set HOOK_SECRET="$(openssl rand -hex 32)"
```

Use the SAME value as the GitHub secret. They must match exactly.

## How the workflow decides what to fire

The workflow registers 6 different `cron:` triggers and
dispatches the right subset of hooks based on which trigger
fired. Inside the job, a bash associative array maps each
schedule string to the comma-separated hook list:

```bash
declare -A SCHEDULE_HOOKS=(
  ["*/10 * * * *"]="poll-gmail-sent,poll-gmail-replies"
  ["*/15 * * * *"]="poll-crm-activity"
  ["*/30 * * * *"]="reconcile-drafts,poll-crm-pipeline"
  ["0 * * * *"]="poll-crm-lists,poll-crm-deals"
  ["0 */6 * * *"]="poll-crm-investors,poll-crm-relationships"
  ["0 6 * * *"]="build-follow-ups"
)
```

Adding a new hook = add it to the right row (or a new row + a
new `cron:` trigger).

## Fail behavior

- One hook failing within a schedule's batch doesn't stop the
  rest from firing (per-hook loop with `FAIL=1` flag).
- The job exits non-zero if ANY hook returned non-200. You'll
  see a red ❌ in the Actions tab.
- `concurrency: scheduler / cancel-in-progress: false` means a
  late-finishing run blocks the next one from starting (instead
  of piling up). With 10-min schedules and ~30s per-hook latency
  this is fine.

## Manual trigger for testing

Go to Actions → Scheduler → "Run workflow" and type the hook
path (e.g. `poll-gmail-sent`). It fires only that hook.

Or from the CLI:

```bash
gh workflow run scheduler.yml -f hook=poll-gmail-sent
```

## GitHub free-tier caveats

- Scheduled workflows can be delayed under platform load (no
  SLA on cron timing). 10-minute hooks may actually fire every
  11-12 minutes during busy periods.
- Scheduled runs are skipped if the repo has had no activity
  for 60 days. As long as the repo is active this isn't an
  issue.
- 2000 minutes/month free; this workflow uses ~3 min/hour of
  runner time = ~100 min/day = ~3000/month. Trim to the most
  critical hooks if you hit the cap, or upgrade.

## Why not Fly Machines schedules?

`fly machine run --schedule daily ...` works but requires:
- `flyctl` access on whoever wires it up
- A separate machine per schedule (or one machine doing internal scheduling)
- Fly-specific config that doesn't survive a switch to a different host

GitHub Actions schedules work for any deployment target, ship
in this repo as code, and the operator never needs to learn Fly
internals. Trade-off: GH cron timing is less precise.

If you need stricter timing, replace this workflow with a Fly
Machines schedule by running `fly machine run` with `--schedule`
flags. The hook endpoints don't care who calls them.
