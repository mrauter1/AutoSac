# AutoSac Stage 1

AutoSac Stage 1 is a server-rendered support triage application. Requesters can open tickets with image attachments, ops users can review and respond through the browser, and a background worker can classify tickets and draft safe replies within a tightly scoped read-only workspace.

This repository ships the Stage 1 contract only:
- FastAPI web app with Jinja templates and HTMX-enhanced ops views
- PostgreSQL-first schema managed by Alembic, with SQLite used in smoke tests
- Background worker that calls the Codex CLI against a read-only workspace mount
- Browser login, CSRF protection, requester and ops role separation, and attachment validation

Stage 1 does not include Slack or email delivery, OAuth or SSO, a SPA frontend, OCR, non-image attachments, worker web search, repo writes by the Codex worker, or automated patch generation.

## Stack

- Python 3.11+
- FastAPI and Starlette
- SQLAlchemy 2.x and Alembic
- Jinja2 templates plus vendored HTMX
- argon2-cffi password hashing
- Codex CLI for ticket triage runs

## Environment

Copy [`.env.example`](/workspace/AutoSac/.env.example) and fill in every required value before running the app.

Required variables:
- `APP_BASE_URL`
- `APP_SECRET_KEY`
- `DATABASE_URL`
- `UPLOADS_DIR`
- `TRIAGE_WORKSPACE_DIR`
- `REPO_MOUNT_DIR`
- `MANUALS_MOUNT_DIR`
- `CODEX_BIN`
- `CODEX_API_KEY`
- `CODEX_MODEL`
- `CODEX_TIMEOUT_SECONDS`
- `WORKER_POLL_SECONDS`
- `AUTO_SUPPORT_REPLY_MIN_CONFIDENCE`
- `AUTO_CONFIRM_INTENT_MIN_CONFIDENCE`
- `MAX_IMAGES_PER_MESSAGE`
- `MAX_IMAGE_BYTES`
- `SESSION_DEFAULT_HOURS`
- `SESSION_REMEMBER_DAYS`

Notes:
- `CODEX_MODEL` may be left blank to use the Codex CLI default model selection.
- `REPO_MOUNT_DIR` and `MANUALS_MOUNT_DIR` are mounted into the worker workspace for read-only analysis; Stage 1 instructions explicitly forbid the worker from editing those trees.
- `TRIAGE_WORKSPACE_DIR` is where bootstrap creates `AGENTS.md`, the Stage 1 skill file, the `runs/` directory, and the workspace git repository used by Codex runs.

## Bootstrap Sequence

The bootstrap path is migration-first and deterministic. Run these commands in order:

```bash
python -m alembic -c alembic.ini upgrade head
python scripts/bootstrap_workspace.py
python scripts/create_admin.py \
  --email admin@example.com \
  --display-name "Stage One Admin" \
  --password "change-me-now" \
  --if-missing
```

What each step does:
- `python -m alembic -c alembic.ini upgrade head` creates the schema, including the auth and preauth session tables.
- `python scripts/bootstrap_workspace.py` creates the uploads directory, verifies the repo/manual mounts, bootstraps the triage workspace files, and seeds `system_state.bootstrap_version` plus `system_state.worker_heartbeat`.
- `python scripts/create_admin.py --if-missing ...` creates the initial admin user exactly once. If the normalized email already belongs to an admin, it exits successfully without changing the account. If that email belongs to a non-admin user, it fails closed.

`python scripts/bootstrap_workspace.py` requires migrations to have run first. It is not a schema creation shortcut.

## Running The App

Start the web app:

```bash
python scripts/run_web.py
```

Start the worker in a separate shell:

```bash
python scripts/run_worker.py
```

Browser entry points:
- `/login`
- `/app` for requester views
- `/ops` and `/ops/board` for ops views

Health endpoints:
- `GET /healthz` returns `{"status": "ok"}` when the process is up.
- `GET /readyz` returns `{"status": "ready"}` only when settings validation, database connectivity, and workspace contract checks all pass.

## Smoke Checks

The repository includes deterministic script-level smoke checks:

```bash
python scripts/run_web.py --check
python scripts/run_worker.py --check
```

Expected behavior:
- `python scripts/run_web.py --check` instantiates the app and verifies `GET /healthz` and `GET /readyz`.
- `python scripts/run_worker.py --check` validates the configured database and the required workspace contract paths.
- Both checks fail before the workspace bootstrap is complete, and the web check also fails if readiness prerequisites are missing.

## Workspace Contract

`python scripts/bootstrap_workspace.py` creates a dedicated Stage 1 triage workspace under `TRIAGE_WORKSPACE_DIR` with:
- `AGENTS.md`
- `.agents/skills/stage1-triage/SKILL.md`
- `runs/`
- a git repository initialized for the workspace itself

The worker uses that workspace to run Codex with:
- read-only sandboxing
- web search disabled
- the local repo and manuals mounted as evidence sources
- prompt and result artifacts persisted per run, including `prompt.txt` and `final.json`

Stage 1 worker boundaries remain intentionally narrow:
- no repository writes
- no browsing the public web
- no OCR
- no non-image attachments
- no patch generation

## Tests

Run the full test suite with:

```bash
pytest
```

Targeted validation during setup work is usually:

```bash
pytest tests/test_hardening_validation.py -q
pytest tests/test_auth_requester.py -q
pytest tests/test_ops_workflow.py -q
pytest tests/test_ai_worker.py -q
```

The hardening/validation coverage includes:
- env and README contract checks
- migration-first bootstrap enforcement
- idempotent admin bootstrap
- web and worker smoke checks
- readiness behavior
- system-state default seeding

## Stage 1 Boundaries

Keep the repository expectations aligned with the shipped implementation:
- Stage 1 is browser-based and server-rendered; there is no SPA or session middleware auth layer.
- Ops list and board filtering use HTMX fragments, but ticket read tracking only advances on detail views and existing state-changing actions.
- The Codex worker may inspect `app/` and `manuals/` through the mounted workspace, but it is repo-aware and read-only by contract.
- Automatic public replies are restricted by the worker validation rules; unsupported or unsafe cases fall back to clarification, draft, or routing flows instead of publishing.
