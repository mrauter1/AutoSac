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
   - `CODEX_API_KEY` if the deployment will not already have authenticated Codex CLI access. In most cloud runtimes, set it.
5. Deploy.

## One-time bootstrap after first deploy

Open Render Shell and run:

```bash
alembic upgrade head
python scripts/bootstrap_workspace.py
python scripts/create_admin.py --email admin@example.com --display-name "Admin" --password "change-me-now"
```

Then restart the service.

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

## If you want me to do the actual cloud deploy for you

I can do it from this environment, but I need:
- a Render API key (or Railway token)
- your OpenAI/Codex API key
- a target repo I can push to

Once you share those, I can run the full deployment end-to-end for you.
