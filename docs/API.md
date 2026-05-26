# Investor Outreach API

External JSON API consumed by the `awesome_investor_tool` React
frontend. Wraps the operator workflow (review → approve → export)
that the CLI scripts already implement. Every mutating endpoint
shells out to the matching `scripts/*.py` so the workspace lock +
audit log + backup behave identically to running the commands by
hand.

OpenAPI spec: [`docs/openapi.json`](./openapi.json) (auto-generated;
regenerate with `make api-spec` or the one-liner at the bottom of
this doc).

## Base URL

| Environment | Base URL |
|---|---|
| Local dev | `http://localhost:8080` |
| Fly.io | `https://<app-name>.fly.dev` |

## Auth

Single shared API key in `Authorization: Bearer <API_KEY>`. The
server reads `API_KEY` from env; refusing requests when missing or
mismatched.

```bash
curl -H "Authorization: Bearer $API_KEY" https://.../review/pending
```

## CORS

Two env vars, used together. starlette OR's them — an Origin matches
if it's in the explicit list OR matches the regex.

```bash
# Exact origins (comma-separated):
fly secrets set CORS_ORIGINS=https://app.your-domain.com,https://staging.your-domain.com

# OR a regex (use this when the frontend rotates preview URLs):
fly secrets set CORS_ORIGIN_REGEX='https://([a-z0-9-]+--)?<project-id>\.(lovable\.app|lovableproject\.com)'
```

Wildcard `*` only fires when neither env var is set — local-dev
shape. Production must pin one or both. The regex is the right
answer for hosts like Lovable that spawn ephemeral preview
subdomains per session.

## Endpoints

All endpoints require the `Authorization` header. Mutations return
`{ok: true, stdout, stderr}` on success or `400` with
`{detail: {error, stdout, stderr}}` on a CLI refusal.

### Review

#### `GET /review/pending`
Drafts waiting for operator review. Each row carries the live
approval-gate readout so the frontend can render hard/soft
blockers inline.

Response: `DraftView[]` with `gate: {ok, blockers: [{text, severity}], overridden: []}`

#### `GET /drafts/approved`
Drafts already in `approved_to_send`. `gate` is null here (the
queue is post-gate); the live re-check fires on send/export.

### Mutations

#### `POST /drafts/{draft_id}/approve`
```json
{ "notes": "wedge framing matches partner's recent thesis", "override_blockers": false }
```
- `notes` required (operator rationale; recorded for audit).
- `override_blockers=true` forwards `--override-blockers` to the
  CLI. HARD blockers (missing email, DNC, superseded, left_fund,
  inactive fund) can NEVER be overridden — the CLI refuses.

#### `POST /drafts/{draft_id}/reject`
```json
{ "notes": "off-thesis; tone too aggressive" }
```

#### `POST /partners/{partner_id}/email`
```json
{ "email": "alice@fund.example" }
```
Setting an email AFTER an approval automatically stales that
approval (the underlying `set_partner_email.py` does this; see
`core/approval/persistence.stale_live_approvals_for_partner`).

### Status

#### `GET /check_ready?phase=send|gmail|review|attio`
Pre-flight check. Default `phase=send`. Returns `{phase, stdout,
blocked: bool, return_code}`. The frontend can show `stdout`
verbatim — it's already operator-friendly.

#### `GET /runs?limit=50`
Recent run rows (most recent first). Useful for a status panel.

### Export

#### `GET /send_queue.csv`
Builds + streams the send-queue CSV. Returns 400 if there's nothing
approved or if a live blocker has reappeared on an approved row.

## Frontend example

Lovable's `src/lib/api.ts` should look roughly like:

```ts
const API = import.meta.env.VITE_API_BASE_URL!;
const KEY = import.meta.env.VITE_API_KEY!;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${KEY}`,
      "Content-Type": "application/json",
    },
  });
  if (!res.ok) throw await res.json();
  return res.json();
}

export const listPending = () => api<DraftView[]>("/review/pending");
export const approve = (id: number, notes: string, override = false) =>
  api<{ok: true}>(`/drafts/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({notes, override_blockers: override}),
  });
// ... etc
```

## Local development

```bash
uv sync --extra api
API_KEY=dev \
INVESTOR_WORKSPACE=clients/test_workspace \
CORS_ORIGINS=http://localhost:5173 \
  uv run --extra api uvicorn web.api:app --reload --port 8080
```

Interactive docs at `http://localhost:8080/docs`.

## Deploy (Fly.io)

The repo's `Dockerfile` runs uvicorn by default. After deploy:

```bash
fly secrets set API_KEY=$(openssl rand -hex 32)
fly secrets set CORS_ORIGINS=https://app.your-domain.com
fly deploy
```

Set the same `API_KEY` as `VITE_API_KEY` on the frontend deploy.

## Regenerating the OpenAPI spec

When endpoints change:

```bash
API_KEY=dev INVESTOR_WORKSPACE=clients/test_workspace \
  uv run --extra api python -c \
  "import json; from web.api import app; \
   open('docs/openapi.json','w').write(json.dumps(app.openapi(), indent=2))"
```

CI should fail if `docs/openapi.json` is out of date (TODO).
