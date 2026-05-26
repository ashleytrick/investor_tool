# FastAPI Backend

FastAPI backend for the external React/Lovable frontend. It wraps the existing
CLI pipeline and operator commands, so workspace locks, backups, migrations, run
logs, and approval gates stay in the same Python code paths used by the CLI.

This is the browser API surface. The older static `ui_prototype/` is mock data;
this backend is the part a real frontend talks to.

## Local Development

```bash
uv sync --extra api
API_KEY=dev-key \
INVESTOR_WORKSPACE=clients/test_workspace \
API_ALLOW_EXAMPLE_DOMAINS=true \
  uv run --extra api uvicorn web.api:app --reload --port 8080
```

OpenAPI is available at `http://localhost:8080/openapi.json`.

Every non-health endpoint requires:

```http
Authorization: Bearer dev-key
```

## Important Safety Defaults

The API does **not** pass fixture/example-domain bypass flags by default. That
means `.example`, `.test`, `.invalid`, and other fixture data stay blocked in
client-facing deployments.

For local fixture demos only, set:

```bash
API_ALLOW_EXAMPLE_DOMAINS=true
```

Do not set that variable for a client production/pilot deployment unless you are
intentionally demoing fixture data.

Workspace modes exposed by the API are:

- `fixture` - fake/sample data; external actions should be blocked.
- `dry_run` - real workspace setup, external writes still gated off.
- `production` - real operator workflow after readiness checks pass.

## Core Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /config` | Mode, Gmail status, company-profile completion |
| `GET /config/company` | Read onboarding company profile |
| `PUT /config/company` | Write onboarding company profile |
| `POST /config/mode` | Set `fixture`, `dry_run`, or `production` |
| `POST /pipeline/score` | Run Stage 6 scoring |
| `POST /pipeline/generate` | Run Stage 7 draft generation, capped at 10 |
| `GET /review/pending` | Drafts waiting for review |
| `POST /drafts/{draft_id}/approve` | Approve a draft through the CLI gate |
| `POST /drafts/{draft_id}/reject` | Reject a draft |
| `POST /partners/{partner_id}/email` | Set a partner email |
| `GET /check_ready?phase=send|gmail|attio|review` | Readiness gate output |
| `GET /send_queue.csv` | Export approved outreach CSV |
| `GET /gmail/status` | Gmail token status |
| `POST /gmail/connect` | Start browser Gmail OAuth |
| `GET /oauth/gmail/callback` | Google OAuth callback |

## What The API Does Not Yet Do

The current onboarding endpoints run Stage 6 and Stage 7. They do not yet run
Stages 1-5. For a client pilot, either preload the workspace or run the earlier
pipeline stages through the CLI before the client uses the browser UI.

## Gmail OAuth

The browser OAuth callback path on current `main` is:

```text
/oauth/gmail/callback
```

Create a **Web application** OAuth client in Google Cloud Console and register
the deployed callback URL, for example:

```text
https://investor-outreach-api-trick.fly.dev/oauth/gmail/callback
```

Upload the downloaded OAuth JSON to:

```text
/data/workspace/.gmail_credentials.json
```

The API writes the resulting token to:

```text
/data/workspace/.gmail_token.json
```

The CLI and API both read the same token file.

## Deploy To Fly

`fly.toml` is configured for the API app:

```text
investor-outreach-api-trick
```

Required secrets:

```bash
fly secrets set API_KEY='choose-a-strong-random-key' -a investor-outreach-api-trick
fly secrets set CORS_ORIGINS='https://your-frontend-origin' -a investor-outreach-api-trick
fly secrets set ANTHROPIC_API_KEY='sk-ant-...' -a investor-outreach-api-trick
```

Optional:

```bash
fly secrets set ATTIO_API_KEY='...' -a investor-outreach-api-trick
fly secrets set CORS_ORIGIN_REGEX='https://.*\.lovableproject\.com' -a investor-outreach-api-trick
```

Do **not** set `API_ALLOW_EXAMPLE_DOMAINS=true` on a client deployment.

Seed a workspace onto the mounted volume:

```bash
fly ssh sftp shell --app investor-outreach-api-trick
> put -r clients/<your_workspace> /data/workspace
> exit
```

Or seed the fixture for local/demo testing only:

```bash
fly ssh console --app investor-outreach-api-trick \
  -C 'cp -r /app/clients/test_workspace /data/workspace'
```

## Client Pilot Checklist

Before a client uses the browser UI:

1. Workspace exists at `/data/workspace`.
2. `company.yaml` is set to `dry_run` for first pilot.
3. Stages 1-5 have already run, or the workspace is preloaded.
4. Stage 6 and Stage 7 can run from the API.
5. `GET /review/pending` returns draft rows.
6. `check_ready?phase=review` returns usable output.
7. Gmail OAuth is configured only if draft creation is part of the pilot.
8. `API_ALLOW_EXAMPLE_DOMAINS` is unset for real client data.
