# Deployment guide (phone-friendly)

This repo needs both processes running:
- `web`: FastAPI app
- `worker`: background Codex runner

On platforms where shared disks across two services are hard, run both in one service using `bash scripts/start_all.sh`.

## What I changed to make this deploy easier

- Added `scripts/start_all.sh` (starts worker in background, web in foreground).
- Added `render.yaml` blueprint for one-click Render setup.
- Updated `Procfile` to use the combined start script.
- `scripts/run_web.py` already honors `PORT` for PaaS.

## Option A (recommended): Render Blueprint from your phone

1. Push this repo to GitHub.
2. In Render mobile web: **New + → Blueprint**.
3. Pick this repo; Render reads `render.yaml` and creates:
   - one web service (`autosac`) with disk mounted at `/opt/triage`
   - one Postgres database (`autosac-db`)
4. Set secrets when prompted:
   - `APP_BASE_URL` (use your Render URL, e.g. `https://autosac.onrender.com`)
   - `UI_DEFAULT_LOCALE=pt-BR` if you want Portuguese as the server-side fallback UI language
   - `CODEX_API_KEY` if the deployment will not already have authenticated Codex CLI access. In most cloud runtimes, set it.
   - Slack DM settings are DB-backed. After the first admin signs in, use `/ops/integrations/slack` to store the bot token and notify flags. Leave Slack disabled or disconnected there until the migration is live and both web and worker are on the same DM-capable release.
5. Deploy.

## One-time bootstrap after first deploy

Open Render Shell and run:

```bash
alembic upgrade head
python scripts/bootstrap_workspace.py
python scripts/create_admin.py --email admin@example.com --display-name "Admin" --password "change-me-now"
```

Then restart the service.

After the admin account exists, sign in and configure Slack only when you are ready to test it:

- `/ops/integrations/slack` for the bot token, enablement, notify flags, and delivery tuning
- `/ops/users` for user `slack_user_id` mappings

## Validation

```bash
python scripts/run_web.py --check
python scripts/run_worker.py --check
```

And verify:
- `GET /healthz`
- `GET /readyz`

## Option B: Railway

Use one service with start command:

```bash
bash scripts/start_all.sh
```

Then set the same env vars from `.env.example`, plus persistent storage mounted to `/opt/triage`.

Slack rollout posture is the same on Railway: deploy the web request path and worker together, verify the migration first, then enable Slack from `/ops/integrations/slack` only after an admin can also populate `/ops/users` Slack IDs.

If the environment already contains earlier dry-run Slack integration rows, treat that Slack-specific state as disposable pre-launch data before enabling Slack. Those rows are not a compatibility target for the DM delivery worker.

Rollback posture is also config-first: disable Slack or disconnect the bot token on `/ops/integrations/slack` without deleting the integration tables or mutating stored event state. Turning Slack back on later still does not backfill old ticket activity; only new events emitted after re-enable can create fresh target rows.

## If you want me to do the actual cloud deploy for you

I can do it from this environment, but I need:
- a Render API key (or Railway token)
- your OpenAI/Codex API key
- a target repo I can push to

Once you share those, I can run the full deployment end-to-end for you.
