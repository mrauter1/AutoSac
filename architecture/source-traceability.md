# Source traceability and documentation notes

## 1. Primary implementation evidence

| Architectural claim | Primary source files |
| --- | --- |
| FastAPI composition, routes, logging, health/readiness | `app/main.py`, `app/routes_auth.py`, `app/routes_requester.py`, `app/routes_ops.py` |
| Server-rendered/HTMX UI and localization | `app/ui.py`, `app/i18n.py`, `app/templates/`, `app/static/htmx.min.js` |
| Authentication, cookies, CSRF and roles | `app/auth.py`, `shared/security.py`, `shared/sessions.py`, `shared/preauth_login.py`, `shared/permissions.py` |
| Ticket aggregate behavior and statuses | `shared/ticketing.py`, `shared/models.py` |
| PostgreSQL session/transaction infrastructure | `shared/db.py`, `alembic.ini`, `shared/migrations/env.py` |
| Relational entities and constraints | `shared/models.py`, `shared/migrations/versions/*.py` |
| AI queue ownership and recovery | `worker/main.py`, `worker/queue.py`, `worker/run_ownership.py` |
| Router/selector/specialist orchestration | `worker/pipeline.py`, `shared/routing_registry.py`, `agent_specs/registry.json` |
| Prompt/spec/skill system | `shared/agent_specs.py`, `worker/prompt_renderer.py`, `agent_specs/*` |
| Codex command, sandbox, artifacts and validation | `worker/step_runner.py`, `worker/artifacts.py`, `worker/output_contracts.py` |
| Publication and stale-input protection | `worker/triage.py`, `worker/publication_policy.py` |
| Upload validation/storage | `app/uploads.py`, requester/ops route call sites, `worker/step_runner.py` |
| Slack outbox event emission | `shared/integrations.py`, ticketing call sites |
| Slack DB settings/token encryption/API | `shared/slack_dm.py`, `shared/models.py` |
| Slack target claiming/retry/delivery | `worker/slack_delivery.py` |
| Slack user directory sync | `shared/slack_user_sync.py`, `worker/slack_user_sync.py` |
| Workspace contract/bootstrap | `shared/contracts.py`, `shared/workspace.py`, `scripts/bootstrap_workspace.py` |
| Service startup/deployment | `scripts/run_web.py`, `scripts/run_worker.py`, `scripts/start_all.sh`, `scripts/setup_systemd_services.sh`, `render.yaml`, `Procfile` |
| Superloop orchestration | `superloop/superloop.py`, `superloop/loop_control.py`, `superloop/templates/` |
| Regression coverage | `tests/`, `superloop/tests/` |

## 2. HTTP surface

### Public/process endpoints

- `GET /` -> `/app`
- `GET /healthz`
- `GET /readyz`
- `GET|POST /login`
- `POST /logout`
- `GET /ui-language`

### Requester surface

- `GET /app`, `GET /app/tickets`
- `GET /app/tickets/new`, `POST /app/tickets`
- `GET /app/tickets/{reference}`
- `POST /app/tickets/{reference}/reply`
- `POST /app/tickets/{reference}/resolve`
- `GET /attachments/{attachment_id}`

### Operations/admin surface

- `GET /ops`, `GET /ops/board`
- `GET /ops/tickets/{reference}`
- `POST /ops/tickets/{reference}/assign`
- `POST /ops/tickets/{reference}/set-status`
- `POST /ops/tickets/{reference}/reply-public`
- `POST /ops/tickets/{reference}/note-internal`
- `POST /ops/tickets/{reference}/rerun-ai`
- `POST /ops/drafts/{draft_id}/approve-publish`
- `POST /ops/drafts/{draft_id}/reject`
- `GET /ops/users`
- `POST /ops/users/create`
- `POST /ops/users/{user_id}/update`
- `POST /ops/users/{user_id}/set-active`
- `GET|POST /ops/integrations/slack`
- `POST /ops/integrations/slack/disconnect`

`shared/contracts.APP_ROUTES` contains the original core route contract but does not list newer user-management, Slack-management, or locale endpoints. The route decorators are the authoritative current surface.

## 3. Documentation caveats discovered

The report resolves conflicts in favor of executable source:

- `superloop/Readme.md` still shows older copied prompt files, `run_log.md`, `summary.md`, `review_findings.md`, and `test_gaps.md`. Current `superloop.py` constants and workspace functions implement shared templates, `decisions.txt`, raw/event logs, and reduced phase-local artifact sets. The report documents current code.
- Root `README.md` calls the application “Stage 1” and accurately summarizes the web/worker shape, but does not document every module or current endpoint.
- The root numeric `AUTO_*_MIN_CONFIDENCE` settings remain configured, but current publication decisions use categorical confidence/risk thresholds from `agent_specs/registry.json`.
- Upload setting names refer to images, while current upload validation intentionally accepts arbitrary files and only uses Pillow to enrich recognized images.
- `worker/codex_runner.py` is not an alternative current runner. It contains compatibility failures and re-exports only command construction.
- Product requirement files under `tasks/` record design evolution and should not be treated as runtime dependencies.

## 4. Scope and freshness

This is a static architecture report for the checked-out source. It does not inspect a live database, deployed environment, Slack workspace, Codex account, or generated triage workspace. Consequently:

- runtime row volumes, latency, deployment health and actual environment values are out of scope;
- the ER model represents the latest ORM/migration source, not a verified live schema;
- external service behavior is described from adapters and tests, not live calls;
- ignored/untracked runtime directories such as `.autoloop/` and `.superloop/` are not production AutoSac modules.

The repository was clean on branch `dev` when inspected. The last committed source revision was `22af53afb980e4fbfff3cc9c05af979707fd2ade` dated 2026-05-19.

## 5. Suggested maintenance rule

Update this report when any of these contracts change:

- ORM entities or Alembic head;
- route decorators or role permissions;
- ticket/run/delivery status values;
- `agent_specs/registry.json`, agent manifests, or output contracts;
- workspace/run artifact layout;
- web/worker process topology or startup scripts;
- Slack event types, recipient rules or API methods;
- Superloop artifact/session/control protocol.

A lightweight review can compare these source files against the claim table above and update only the affected report sections and Mermaid sources.

