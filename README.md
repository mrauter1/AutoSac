# AutoSac Stage 1

AutoSac Stage 1 is an internal ticket triage app built around server-rendered FastAPI pages, PostgreSQL-backed server-side sessions, and a separate worker that runs Codex in a read-only workspace. The product scope is intentionally narrow:

- Requesters create tickets, reply, and resolve their own requests.
- Dev/TI and admins work the queue from `/ops` and `/ops/board`.
- The worker classifies tickets, asks clarifying questions, drafts replies, or routes work to Dev/TI.
- Each AI run keeps `prompt.txt`, `schema.json`, `stdout.jsonl`, `stderr.txt`, and `final.json`; `final.json` is the canonical output contract.

## Runtime Shape

- Web app: FastAPI + Jinja templates + local static assets.
- Auth: opaque server-side session cookie plus a separate short-lived preauth login cookie for `/login` CSRF.
- Database: PostgreSQL in normal operation, managed through Alembic migrations.
- Worker: polls pending AI runs, seeds missing `system_state` defaults, then processes Codex runs from the mounted workspace.
- HTMX: vendored locally at `/static/htmx.min.js`; `/ops` and `/ops/board` return full HTML for normal navigation and fragment responses for HTMX filter refreshes.

Unauthenticated browser navigation to protected HTML pages redirects to `/login` with a sanitized relative `next` value. Authenticated users with the wrong role still receive `403`.

## Local Setup

1. **Create and activate a virtual environment (strongly recommended).**

   Using a venv prevents dependency/version conflicts with system Python and other projects.

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   ```

   > Strong advice: do all local development and script execution for this project inside the activated venv.

2. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Export the variables from `.env.example`.

   Required values:
   - `APP_BASE_URL`
   - `APP_SECRET_KEY`
   - `DATABASE_URL`
   - `CODEX_BIN`
   - `CODEX_API_KEY`

4. Ensure the workspace mount directories exist before bootstrapping:
   - `REPO_MOUNT_DIR`
   - `MANUALS_MOUNT_DIR`

5. Apply the schema:

   ```bash
   alembic upgrade head
   ```

## Deterministic Bootstrap Flow

Run these steps in order on a new environment:

1. Bootstrap the workspace files and runs directory:

   ```bash
   python scripts/bootstrap_workspace.py
   ```

   The script creates the workspace contract files, initializes the workspace git repository if needed, and prints a JSON snapshot that includes `"bootstrap_version": "stage1-v1"`.

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

- `python scripts/run_web.py --check` verifies `/healthz` and `/readyz`.
- `python scripts/run_worker.py --check` verifies database connectivity, workspace contract paths, and the configured worker poll interval.

Useful endpoints:

- `GET /healthz`
- `GET /readyz`

## Notes For Operators

- `.env.example` lists every supported runtime knob and the shipped defaults.
- `APP_BASE_URL=https://...` automatically enables secure cookies.
- `/ops` and `/ops/board` filter refreshes do not mark tickets as viewed; ticket detail pages still do.
- The web and worker processes expect the same database and workspace configuration.

## Deployment

For a phone-friendly cloud deployment walkthrough and one-click Render blueprint setup, see `docs_deployment.md` and `render.yaml`.

## Tests

Run the regression suite with:

```bash
pytest
```
