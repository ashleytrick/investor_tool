# Operator Web UI

Streamlit app over the existing CLI pipeline. Lets you browse the
review queue, set partner emails, approve/reject drafts, run
`check_ready --for send`, and download the send-queue CSV without
ever leaving the browser. Every mutating action shells out to the
matching `scripts/*.py` so the existing lock + audit + backup story
is unchanged.

## Local

```bash
uv sync --extra web
APP_PASSWORD=dev \
INVESTOR_WORKSPACE=clients/test_workspace \
  uv run --extra web streamlit run web/app.py
```

Then open http://localhost:8501.

## Deploy to Fly.io (free tier)

Fly's free allotment covers 3 shared-CPU-1x VMs + 3GB of persistent
volume -- enough for one operator UI with the SQLite workspace on
disk. A credit card is required at signup but you won't be billed
while you stay under the free limits.

One-time setup:

```bash
# 1. Install + login.
curl -L https://fly.io/install.sh | sh
fly auth signup            # or: fly auth login

# 2. Pick a unique app name. Edit fly.toml line 1:
#    app = "investor-outreach-ui-<yourname>"

# 3. Provision the app + persistent volume.
fly launch --no-deploy --copy-config --name <your-app-name>
fly volumes create investor_workspace --size 3 --region iad

# 4. Set secrets (NOT in fly.toml -- those land in env, secrets stay
#    encrypted at rest and are injected at runtime).
fly secrets set APP_PASSWORD='choose-a-strong-password'
fly secrets set ANTHROPIC_API_KEY='sk-ant-...'    # if Stage 7 used in UI
fly secrets set ATTIO_API_KEY='...'               # if Stage 8 used in UI

# 5. Deploy.
fly deploy
```

The app comes up at `https://<your-app-name>.fly.dev`. Hit it, enter
the password, you're in.

## Seeding a workspace on Fly

The UI expects a workspace at `/data/workspace` (mounted from the
persistent volume). On first deploy that's empty, so seed it:

```bash
# Push your workspace config from your laptop to the volume.
fly ssh sftp shell
> put -r clients/<your_workspace> /data/workspace
> exit

# Or, for the fixture workspace to play around:
fly ssh console -C 'cp -r /app/clients/test_workspace /data/workspace'
```

Then re-load the UI. Use the "Runs" tab to verify the workspace was
picked up.

## Running pipeline stages from Fly

The web UI covers the operator launch path (review, approve, export,
check_ready). The earlier pipeline stages (01-07) are still CLI-only
for now; run them on the Fly machine:

```bash
fly ssh console
cd /app
uv run scripts/06_score_candidates.py --workspace /data/workspace
uv run scripts/07_generate_emails.py --workspace /data/workspace --top 25
exit
```

The refresh in the UI will show the new drafts.

## Logs + debugging

```bash
fly logs           # follow live
fly status         # machine state
fly ssh console    # shell in
```
