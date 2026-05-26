# Gmail OAuth — web-flow setup for the deployed API

The dashboard's "Connect Gmail" button (`POST /gmail/connect` →
`GET /gmail/callback` round-trip) needs a Google Cloud Console
OAuth client of type **Web application** with the deployed
callback URL registered. The pre-existing `scripts/connect_gmail.py`
flow uses a **Desktop**-type client that only works on localhost —
those credentials WILL NOT work for the deployed API.

You can keep both clients (Desktop + Web) on the same GCP project
side-by-side. They share the consent screen, the same Gmail API
enablement, and the same test-user list; only the client_id / 
client_secret differ.

## One-time GCP setup

1. **Open Google Cloud Console** at https://console.cloud.google.com/
   and pick (or create) the project you used for the desktop client.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
3. **Application type**: `Web application`.
4. **Name**: anything (e.g. `investor-outreach-web`).
5. **Authorized redirect URIs** — add one entry for each environment
   you'll connect from:
   - `https://investor-outreach-api-trick.fly.dev/gmail/callback`  ← prod
   - `http://localhost:8080/gmail/callback`  ← if you'll test locally
6. **Create**, then click **Download JSON** on the new client row.

You'll get a file like `client_secret_NNNN-...apps.googleusercontent.com.json`.
Rename it to `web_oauth_client.json` to keep it distinct from the
Desktop one.

## Upload the JSON to the workspace volume

The deployed API looks for the OAuth client at
`<workspace>/.gmail_credentials.json` (same path the existing
GmailClient reads). Push your downloaded JSON there:

```bash
# Replace ~/Downloads/web_oauth_client.json with your actual path.
fly ssh sftp shell --app investor-outreach-api-trick
# In the SFTP shell:
put ~/Downloads/web_oauth_client.json /data/workspace/.gmail_credentials.json
exit
```

Verify:

```bash
fly ssh console --app investor-outreach-api-trick \
  -C "ls -la /data/workspace/.gmail_credentials.json"
```

Should print a 1-2 KB file owned by root.

## Optional environment overrides

By default the API derives the callback URL from the incoming
request (so localhost dev "just works"). If you're running behind
a reverse proxy that rewrites the scheme or host, pin the URL
explicitly:

```bash
fly secrets set GMAIL_OAUTH_REDIRECT_URI=https://investor-outreach-api-trick.fly.dev/gmail/callback
```

Also configure where the user lands after they finish OAuth (the
"return to dashboard" target). Default is the first entry of
`CORS_ORIGINS`. Override:

```bash
fly secrets set GMAIL_OAUTH_RETURN_URL=https://<your-frontend>/onboarding
```

## End-to-end flow (what the user sees)

1. Dashboard shows "Connect Gmail" button.
2. User clicks it → frontend calls `POST /gmail/connect`.
3. API returns `{auth_url, state}`; frontend does `window.location = auth_url`.
4. Google shows the consent screen; user picks their Gmail account.
5. Google redirects browser to `https://<api>/gmail/callback?code=...&state=...`.
6. API exchanges code for tokens, writes `<workspace>/.gmail_token.json`,
   and bounces the browser back to the frontend with
   `?gmail_connected=1&email=...`.
7. Frontend polls `GET /gmail/status` → `{connected: true}`.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `412 gmail_credentials_missing` from `POST /gmail/connect` | `.gmail_credentials.json` not on the volume | Re-do the SFTP upload step. |
| Google shows "redirect_uri_mismatch" | URL in the GCP client doesn't exactly match the deployed callback | Add the exact URL (scheme + host + path) to the GCP client's Authorized redirect URIs and try again. |
| `400 state_expired` on callback | User took >10 min between Connect click and consent | Click Connect again; states TTL out. |
| `400 exchange_failed` | Wrong client JSON (Desktop instead of Web), scope mismatch, or revoked consent | Re-download a Web-type client JSON, re-upload. |
| Token exchange succeeds but `GET /gmail/status` still returns `connected: false` | Token didn't write to the right path | Check `<workspace>/.gmail_token.json` exists; permissions should be readable by the API process. |

## Token rotation / disconnect

The token file lives at `<workspace>/.gmail_token.json` on the
persistent volume. To force a re-link (e.g. switching the
connected Gmail account):

```bash
fly ssh console -C "rm /data/workspace/.gmail_token.json"
```

`/gmail/status` will then return `connected: false`; the dashboard
shows the Connect button again.
