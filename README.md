# AutoSac Stage 1

AutoSac Stage 1 is an internal ticket triage app built around server-rendered FastAPI pages, PostgreSQL-backed server-side sessions, and a separate worker that runs Codex in a read-only workspace. The product scope is intentionally narrow:

- Requesters create tickets, reply, and resolve their own requests.
- Dev/TI and admins work the queue from `/ops` and `/ops/board`.
- The worker classifies tickets, asks clarifying questions, drafts replies, or routes work to Dev/TI.
- Each AI run now records step-level artifacts under `runs/<ticket>/<run>/`, including router and specialist step folders with `prompt.txt`, `schema.json`, `stdout.jsonl`, `stderr.txt`, and `final.json`; the run also persists the final structured output in the database.

## Runtime Shape

- Web app: FastAPI + Jinja templates + local static assets.
- Auth: opaque server-side session cookie plus a separate short-lived preauth login cookie for `/login` CSRF.
- Database: PostgreSQL in normal operation, managed through Alembic migrations.
- Worker: polls pending AI runs, seeds missing `system_state` defaults, then processes Codex runs from the mounted workspace.
- Worker liveness: each running AI run records `started_at`, worker PID, a per-process worker instance id, and a per-run heartbeat timestamp so stale `running` rows can be recovered safely.
- HTMX: vendored locally at `/static/htmx.min.js`; `/ops` and `/ops/board` return full HTML for normal navigation and fragment responses for HTMX filter refreshes.

Unauthenticated browser navigation to protected HTML pages redirects to `/login` with a sanitized relative `next` value. Authenticated users with the wrong role still receive `403`.

## Local Setup

1. **Create and activate a virtual environment (strongly recommended).**

   Using a venv prevents dependency/version conflicts with system Python and other projects.

   ```bash
   python -m venv .venv
   # On macOS/Linux:
   source .venv/bin/activate

   # On Windows, use .venv\Scripts\activate.bat (cmd) or .venv\Scripts\Activate.ps1 (PowerShell)
   python -m pip install --upgrade pip
   ```

   > Strong advice: do all local development and script execution for this project inside the activated venv.

2. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Create `.env` from `.env.example`.

   Required values:
   - `APP_BASE_URL`
   - `APP_SECRET_KEY`
   - `DATABASE_URL`
   - `CODEX_BIN`

   Optional:
   - `CODEX_API_KEY` if this runtime is not already authenticated via Codex CLI login.
   - Slack DM settings are DB-backed. Configure them later from `/ops/integrations/slack`; there are no authoritative `SLACK_*` runtime env vars in `.env`.

   Runtime scripts load `.env` automatically from the repository root.

4. Ensure the workspace mount directories exist before bootstrapping:
   - `REPO_MOUNT_DIR`
   - `MANUALS_MOUNT_DIR`

5. Optional local preflight/setup:

   ```bash
   python scripts/preflight_setup.py --ensure-workspace-dirs --setup-postgres-local
   ```

   This can create the local workspace mount directories and, when `DATABASE_URL` points at localhost PostgreSQL, call `scripts/setup_postgres_local.sh` to install/start PostgreSQL and create the configured role/database.

6. Apply the schema:

   ```bash
   alembic upgrade head
   ```

7. Backfill historical AI runs into the step-based structure:

   ```bash
   python scripts/backfill_ai_run_steps.py
   ```

## Deterministic Bootstrap Flow

Run these steps in order on a new environment:

1. Bootstrap the workspace files and runs directory:

   ```bash
   python scripts/bootstrap_workspace.py
   ```

   The script creates the workspace contract files, syncs all required AI skills, initializes the workspace git repository if needed, and prints a JSON snapshot that includes `"bootstrap_version": "stage1-v2"`.

2. Create the initial admin account:

   ```bash
   python scripts/create_admin.py --email admin@example.com --display-name "Admin" --password "change-me"
   ```

   This step is deterministic and idempotent:
   - Missing admin: created.
   - Matching existing admin: succeeds without changing state.
   - Conflicting existing record: fails explicitly instead of mutating the user in place.

3. Create any additional local users:

   ```bash
   python scripts/create_user.py --email requester@example.com --display-name "Requester" --password "change-me" --role requester
   python scripts/create_user.py --email devti@example.com --display-name "Dev TI" --password "change-me" --role dev_ti
   ```

4. Start services:

   ```bash
   python scripts/run_web.py
   python scripts/run_worker.py
   ```

The worker initializes missing `system_state` defaults, including `bootstrap_version`, before heartbeat emission and queue processing. The admin bootstrap script seeds the same defaults before user creation.

## User Management CLI

- Create another user:

  ```bash
  python scripts/create_user.py --email user@example.com --display-name "Example User" --password "change-me" --role requester
  ```

- Rotate a password:

  ```bash
  python scripts/set_password.py --email user@example.com --password "new-secret"
  ```

- Deactivate a user:

  ```bash
  python scripts/deactivate_user.py --email user@example.com
  ```

## Smoke Checks

Run these before starting services in a fresh environment or after changing config:

```bash
python scripts/run_web.py --check
python scripts/run_worker.py --check
```

Expected checks:

- `python scripts/run_web.py --check` verifies `/healthz`, `/readyz`, and that AI run history has been fully backfilled into the structured pipeline shape.
- `python scripts/run_worker.py --check` verifies database connectivity, workspace contract paths, worker configuration, and that AI run history has been fully backfilled.

Useful endpoints:

- `GET /healthz`
- `GET /readyz`

## Slack Integration

AutoSac includes DB-backed Slack DM notifications. Ticket mutations on the web/request path record integration events in PostgreSQL, and the worker reloads Slack settings each cycle before delivering eligible requester/assignee DMs through Slack Web API.

Covered event types:

- `ticket.created`
- `ticket.public_message_added`
- `ticket.status_changed`

Admin workflow:

- Configure enablement, the bot token, notify flags, and delivery tuning at `/ops/integrations/slack`.
- Set per-user `slack_user_id` mappings from `/ops/users`.
- The worker validates credentials with `auth.test`, opens DMs with `conversations.open`, and sends message text with `chat.postMessage`.

Operational notes:

- Event recording still happens while Slack is disabled; the stored routing result reflects the suppression reason and no delivery target row is created.
- Delivery is asynchronous and worker-driven, with retry, stale-lock recovery, and dead-letter handling in PostgreSQL-backed integration tables.
- There are no authoritative `SLACK_*` runtime env vars for Slack DM routing or delivery; `APP_SECRET_KEY` is used only to encrypt and decrypt the stored bot token.
- Slack does not backfill historical ticket activity. Only newly emitted events can create new Slack delivery targets.
- Deploy the web request path and worker together before enabling Slack. The rollout treats any pre-launch Slack integration rows from earlier dry runs as disposable data.

For the full DM contract, see `tasks/slack_dm_integration_PRD.md`. The original webhook PRD remains the payload-snapshot reference in `tasks/slack_integration_PRD.md`.

## Notes For Operators

- `.env.example` lists every supported runtime knob and the shipped defaults.
- `APP_BASE_URL=https://...` automatically enables secure cookies.
- `UI_DEFAULT_LOCALE=pt-BR` makes Portuguese the server-side fallback when there is no saved language cookie and no matching browser language.
- Leave `CODEX_API_KEY` empty to rely on existing Codex CLI login in local environments.
- Slack DM delivery is DB-backed and disabled by default until an admin stores a bot token and enables it at `/ops/integrations/slack`.
- Deploy the web request path and worker together as the same DM-capable build before enabling Slack; mixed-version Slack delivery compatibility is not supported for this pre-launch rollout.
- Slack rollback is config-first: disable delivery or disconnect the bot token from `/ops/integrations/slack` while preserving the stored integration rows for later inspection or re-enable.
- `APP_SECRET_KEY` participates in Slack only for bot-token encryption and decryption. There are no authoritative `SLACK_*` runtime env vars.
- Admins manage per-user Slack IDs from `/ops/users`.
- If a pre-launch environment still has earlier dry-run Slack integration rows, treat that Slack-specific state as disposable pre-launch data before enabling Slack.
- Re-enabling Slack later does not backfill historical ticket activity; only newly emitted events can create new target rows.
- Undecryptable stored Slack tokens surface as invalid config until an admin saves a new token.
- On Ubuntu 24.04, Codex read-only probing may require an AppArmor profile for `bwrap`; see the Ubuntu internal server guide.
- `scripts/setup_postgres_local.sh` is intended for local localhost PostgreSQL only; it is not part of the cloud deployment path.
- `WORKER_HEARTBEAT_SECONDS`, `AI_RUN_STALE_TIMEOUT_SECONDS`, and `AI_RUN_MAX_RECOVERY_ATTEMPTS` control stale-run detection and automatic recovery.
- After `alembic upgrade head`, run `python scripts/backfill_ai_run_steps.py` before relying on `/readyz` or the service smoke checks.
- `/ops` and `/ops/board` filter refreshes do not mark tickets as viewed; ticket detail pages still do.
- The web and worker processes expect the same database and workspace configuration.

## Deployment

For a step-by-step Ubuntu internal server setup with boot-time systemd services and the Ubuntu 24.04 AppArmor / `bwrap` fix for Codex sandboxing, see `docs/ubuntu_internal_server_setup.md`.

For a phone-friendly cloud deployment walkthrough and one-click Render blueprint setup, see `docs_deployment.md` and `render.yaml`.

## Tests

Run the regression suite with:

```bash
pytest
```
