# Deployment, operations, security, and quality

## 1. Technology stack

| Layer | Technology |
| --- | --- |
| Language/runtime | Python 3 (version not pinned in repository metadata) |
| Web | FastAPI, Uvicorn, Jinja2, python-multipart |
| Browser | Server-rendered HTML, local CSS, vendored HTMX |
| Persistence | SQLAlchemy 2, PostgreSQL via psycopg 3, Alembic |
| Validation | Pydantic 2 and database constraints |
| Security | Argon2, PBKDF2 compatibility, cryptography/Fernet/HKDF, opaque tokens |
| Content | markdown-it-py, Bleach, Pillow |
| HTTP integration | httpx |
| AI | External `codex` CLI subprocess |
| Testing | pytest, FastAPI/TestClient/httpx |
| Superloop config | Optional PyYAML; required when parsing YAML config/phase plans |

Dependencies use bounded major-version ranges in `requirements.txt`, but there is no lock file. PyYAML is not listed in the root requirements even though Superloop conditionally uses it; a Superloop deployment that reads YAML must provide it separately.

## 2. Configuration ownership

### Required environment values

| Variable | Purpose |
| --- | --- |
| `APP_BASE_URL` | External origin, safe ticket URLs, and Secure-cookie decision |
| `APP_SECRET_KEY` | Application secret and derivation source for Slack token encryption |
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL |
| `CODEX_BIN` | Codex executable name or absolute path |

### Optional environment groups

- Workspace: `TRIAGE_WORKSPACE_DIR`, `UPLOADS_DIR`, `REPO_MOUNT_DIR`, `MANUALS_MOUNT_DIR`.
- Codex/worker: `CODEX_API_KEY`, `CODEX_MODEL`, timeouts, polling, heartbeat and recovery budget.
- Publication: two legacy-looking numeric threshold settings are loaded (`AUTO_SUPPORT_REPLY_MIN_CONFIDENCE`, `AUTO_CONFIRM_INTENT_MIN_CONFIDENCE`), while the current registry pipeline uses categorical per-route publication policy. They remain part of `Settings` but are not the current publication decision source.
- Uploads: count and byte limits; names still use `MAX_IMAGES_*` though arbitrary files are accepted.
- Sessions: default hours and remember days.
- UI: English or `pt-BR` fallback.

Slack enablement, bot token, event flags and delivery tuning are authoritative in the singleton `slack_dm_settings` table and managed at `/ops/integrations/slack`. They are deliberately not environment-based.

Configuration validation enforces positive timings/sizes, stale-run timeout greater than heartbeat interval, nonnegative recovery attempts, supported locale, and `UPLOADS_DIR` containment within the triage workspace.

## 3. Startup and bootstrap order

For a new environment:

1. Install Python requirements and the Codex CLI.
2. Configure environment and create the workspace/mount directories.
3. Ensure PostgreSQL is reachable.
4. Run `alembic upgrade head`.
5. Run `scripts/backfill_ai_run_steps.py` for historical compatibility.
6. Run `scripts/bootstrap_workspace.py` to write exact workspace contracts and skills and initialize Git.
7. Create the first admin.
8. Run both smoke checks.
9. Start web and worker.

The worker and admin bootstrap seed `SystemState` defaults. Readiness deliberately fails when terminal historical runs lack a pipeline version, backfilled legacy runs lack steps, or accepted runs lack structured output.

## 4. Supported deployment shapes

### Separate host services

`scripts/setup_systemd_services.sh` installs `autosac-web.service` and `autosac-worker.service` under a non-root user with a shared repository, `.env`, database and workspace. Both restart automatically. This provides independent process supervision but not independent source/config versions.

### Combined PaaS service

`Procfile` and `render.yaml` call `scripts/start_all.sh`. It backgrounds the worker and execs the web process, terminating the worker when the web process exits. Render provisions one persistent disk at `/opt/triage` and PostgreSQL.

Operational consequences:

- one service instance contains both roles;
- horizontal web scaling also starts more worker processes;
- database locking makes duplicate AI/Slack claims safe, but AI throughput and resource use change with instance count;
- attachment/run storage must be shared or consistently mounted across all instances;
- the combined shell script supervises only the web process directly; platform restart is relied on if the worker exits independently.

## 5. Readiness, liveness and observability

| Mechanism | Semantics |
| --- | --- |
| `/healthz` | Web process is serving requests |
| `/readyz` | Settings, DB, workspace contracts/mounts/registry and run history are valid |
| Worker heartbeat state | Worker identity, timestamp, PID and active run ID in `system_state` |
| Per-run heartbeat | Recovery reference for a running AI run |
| Slack delivery health | Last auth/config health snapshot in `system_state` |
| Slack user-sync state | Pending request and last sync outcome in `system_state` |
| JSON logs | Timestamp, service, level, event and contextual fields to stdout |
| Run artifacts | Exact prompts, schemas, model streams, final JSON and manifests |
| Database audit | Status history, AI steps/results, drafts, integration events/targets |

There is no metrics exporter, tracing backend, alerting definition, or log retention policy in the repository. Operators are expected to use platform/systemd logs and the database/artifact records.

## 6. Security model

### Implemented controls

- HTTP-only opaque session and preauth cookies; only hashed tokens in PostgreSQL.
- Separate login-form CSRF challenge and per-session CSRF token.
- SameSite=Lax cookies and automatic Secure flag under HTTPS.
- Argon2 password hashing with constant-time/token comparison practices.
- Central role predicates plus ownership filtering for requester tickets/attachments.
- Sanitized local redirect paths prevent scheme/host and protocol-relative redirects.
- Markdown raw HTML disabled, output sanitized with an allowlist.
- Attachment storage uses generated UUID filenames and bounded extension validation.
- Workspace target paths are checked for containment.
- Codex approval disabled, sandbox read-only, web search disabled, and workspace instructions forbid mutations/data leakage.
- Structured output forbids extra fields and validates route/specialist eligibility.
- Slack token encrypted at rest with a key derived from the app secret.
- Slack HTTP calls do not follow redirects and have configured timeouts.
- Queue ownership and claim tokens prevent stale concurrent finalization.
- Services are designed to run as a non-root user.

### Trust boundaries and residual risks

- TLS termination is external. The Ubuntu internal guide explicitly does not configure HTTPS/reverse proxy; HTTP deployments send credentials/session cookies without transport encryption.
- `APP_SECRET_KEY` rotation makes existing Slack token ciphertext undecryptable; configuration must be saved again.
- Uploaded arbitrary files are stored and exposed for download; there is no malware scanner or content-disposition policy documented in the architecture.
- The model can read configured repository/manual mounts and public attachment copies. Correct mount contents and OS permissions are part of the security boundary.
- Database and filesystem updates are not one atomic resource transaction.
- Application rate limiting, account lockout, MFA and session revocation-all are not implemented.
- Slack scope minimization and bot installation governance are operational responsibilities.

## 7. Reliability and concurrency

### AI runs

- Durable pending rows and oldest-first claims.
- Partial unique index prevents overlapping active runs for a ticket.
- Deferred requeue retains the latest trigger/requester/forced specialist request.
- Input fingerprint prevents duplicate analysis and stale publication.
- Worker identity checks guard each step and terminal update.
- Heartbeat-based recovery creates lineage-linked replacements up to a configured budget.

### Slack delivery

- Transactional outbox records are committed with ticket state.
- Dedupe keys make repeated event emission idempotent.
- Each recipient target is independently claimable and retryable.
- Claims use row locking plus a random claim token.
- Exponential backoff, rate-limit response support, stale-lock recovery and dead letters are implemented.
- Event payload snapshots preserve what is delivered even if the ticket later changes.

### Filesystem

- Runs use UUID-separated directories, limiting path collisions.
- Manifests are rewritten as state advances and database rows remain the structured source of status.
- There is no repository-defined retention or garbage collection for attachments, run artifacts, sessions, old integration events or dead letters.

## 8. Migration and compatibility strategy

Alembic revisions form a single linear chain. Major transitions include:

1. initial users/sessions/tickets/messages/attachments/history/views/runs/drafts/system state;
2. preauth login challenges;
3. human-review run status;
4. step-based AI pipeline and structured final output;
5. route-target compatibility and selector steps;
6. removal of legacy `ticket_class`;
7. forced manual-rerun route/specialist metadata;
8. deferred-requeue requester audit;
9. worker ownership and stale recovery;
10. Slack event/link/target outbox;
11. Slack routing/claim metadata;
12. DB-backed Slack DM settings and per-user recipients.

The latest Slack DM migration intentionally deletes pre-launch integration rows before reshaping the target model. Deployment documentation treats those earlier Slack records as disposable, which is an explicit pre-production compatibility decision.

Legacy AI run display/backfill remains supported through `triage_result`, legacy pipeline version constants, `backfill_ai_run_steps.py`, and readiness auditing. `worker.codex_runner` retains failure aliases to make removed entry points fail clearly.

## 9. Quality strategy

The pytest suite exercises the architecture at several levels:

- route/auth/session/CSRF/ownership behavior;
- SQLAlchemy model and domain service state transitions;
- AI queue claim, pipeline steps, policy, artifacts, stale recovery and reruns;
- registry/spec validation and role eligibility;
- Slack event idempotency, recipient selection, claims, retries, dead letters, stale locks, config and sync;
- arbitrary-file upload limits and metadata;
- localization and sanitized presentation;
- Superloop parser, artifact scoping, Git delta enforcement, events and resume.

External boundaries—Codex subprocess, Slack HTTP and many DB interactions—are mocked/faked heavily. The repository does not define CI workflow files, coverage thresholds, formatting, linting, static type checking, or vulnerability scanning. `pytest` is therefore the only repository-declared automated quality command.

## 10. Change-impact guide

| Change | Likely modules / contracts affected |
| --- | --- |
| Add ticket field/status | ORM + migration, `shared.ticketing`, filters/templates/i18n, worker fingerprint/prompts, tests |
| Add route target/specialist | Registry, spec manifest/prompt/skill, workspace bootstrap, output validation, ops rerun UI, tests |
| Change publication behavior | Registry publish policy, `worker.publication_policy`, `worker.triage`, presenters/tests |
| Add integration event | Model constraints + migration, `shared.integrations`, ticket mutation call sites, Slack renderer/delivery tests |
| Change upload rules | Settings, `app.uploads`, both route groups, ticket metadata/storage, Codex materialization, tests |
| Change session/auth | Models/migration, security/session/preauth modules, auth routes/dependencies/templates, tests |
| Change AI artifacts | `worker.artifacts`, `step_runner`, DB path fields/migration, run-history/backfill/readiness, ops UI/tests |
| Change deployment topology | Startup scripts, shared disk/DB assumptions, heartbeat/worker concurrency, docs/Render/systemd config |
| Change Superloop workflow | `superloop.py`, templates, loop-control protocol, phase/task file contracts, Superloop tests; no AutoSac runtime impact |

