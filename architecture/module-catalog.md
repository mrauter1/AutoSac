# Module catalog

## 1. Repository layout

| Path | Architectural role |
| --- | --- |
| `app/` | FastAPI composition, HTTP routes, server-rendered UI, uploads, presentation helpers |
| `shared/` | Domain services, SQLAlchemy model, configuration, auth primitives, routing registry, integrations, workspace contract |
| `worker/` | Durable queue consumer, Codex step runner, AI pipeline, publication policy, recovery, Slack background work |
| `agent_specs/` | Versioned router/selector/specialist manifests, prompts, skills, shared policy, and routing registry |
| `shared/migrations/` | Alembic environment and ordered PostgreSQL schema evolution |
| `scripts/` | Service entry points, bootstrap, readiness, backfill, user administration, and host setup |
| `app/templates/`, `app/static/` | Jinja pages/fragments and vendored browser assets |
| `tests/` | AutoSac web, persistence, pipeline, Slack, upload, i18n, and hardening tests |
| `superloop/` | Independent Codex producer/verifier orchestration CLI, templates, documentation, and tests |
| `tasks/` | Product requirements and design history; not loaded by production code |
| `docs/`, `docs_deployment.md` | Deployment/operator documentation |
| `render.yaml`, `Procfile` | PaaS topology and combined startup command |
| `source_code_dump.txt` | Repository snapshot/reference artifact; not imported at runtime |

## 2. `app` package

| Module | Responsibility | Principal dependencies / consumers |
| --- | --- | --- |
| `app.main` | Creates FastAPI app, mounts static files, includes routers, request logging, `/healthz`, `/readyz`, root redirect, HTTP error translation/login redirects | Imports all route groups plus shared config, DB, workspace and run-history checks; loaded by Uvicorn |
| `app.auth` | FastAPI dependencies for optional/required sessions and users, role gates, CSRF checks, cookie issuance/invalidation, browser login redirects | Uses `shared.sessions`, permissions, models and config; consumed by all routers |
| `app.routes_auth` | Login challenge, login/logout, locale switching | Uses preauth sessions, password verification, safe redirect helpers |
| `app.routes_requester` | Ticket list/create/detail/reply/resolve and attachment download for owners; ops users can also use requester views | Orchestrates uploads, `shared.ticketing`, Slack runtime context, visibility checks, templates |
| `app.routes_ops` | Ops list and board, ticket detail/actions, assignment/status, public/internal replies, manual reruns, draft review, user admin, Slack configuration | Main HTTP coordination module; consumes nearly all shared domain/integration services |
| `app.uploads` | Multipart limits, attachment byte loading, SHA-256, optional image dimension probing, storage writes | Pillow and configured size/count limits; used by requester and ops routes |
| `app.render` | CommonMark-to-HTML rendering with raw HTML disabled and Bleach sanitization | Used when serializing ticket messages for templates |
| `app.timeline` | Loads actors/status history and merges message/status events chronologically | Used by requester and ops detail pages |
| `app.ai_run_presenters` | Normalizes current and legacy AI outputs and route-target labels for UI display | Reads routing registry; used by ops pages |
| `app.ui` | Jinja environment, locale-aware template context, HTMX detection, role navigation, and safe relative redirect paths | Central presentation helper |
| `app.i18n` | English and Brazilian Portuguese strings, locale resolution, label/date helpers, and error-message translation | Used by `app.main`, routes and `app.ui` |

The routes are HTML/form endpoints. There is no application-level REST API versioning or separate client bundle.

## 3. `shared` package

### Foundation and security

| Module | Responsibility |
| --- | --- |
| `shared.config` | Loads `.env`; validates required URLs/secrets/paths/timing; exposes immutable web, worker, and Slack settings. Slack DM operational settings are loaded from the DB, not environment variables. |
| `shared.db` | SQLAlchemy declarative base, PostgreSQL engine/session factory, FastAPI DB dependency, transaction context, and connectivity probe |
| `shared.models` | All ORM entities, allowed enum-like values, constraints, indexes, sequence, and foreign keys |
| `shared.contracts` | Cookie names, route/CLI constants, default workspace paths, bootstrap version, and the read-only workspace `AGENTS.md` content |
| `shared.security` | UTC clock, Argon2 password hashing with PBKDF2 compatibility fallback, opaque token/CSRF generation, SHA-256 token hashing, session expiry |
| `shared.sessions` | Creates, resolves, refreshes and invalidates server-side authenticated sessions |
| `shared.preauth_login` | Ten-minute server-side login-form challenge sessions and cleanup |
| `shared.permissions` | Role constants and ticket/ops/admin access predicates |
| `shared.logging` | JSON stdout logging for web, worker, and integration events |

### Domain and administration

| Module | Responsibility |
| --- | --- |
| `shared.ticketing` | Transactional ticket aggregate operations: references, messages, status history, views, attachments, AI-run enqueue/requeue, AI publication/drafts, ops actions, integration event emission, system-state defaults |
| `shared.user_admin` | Normalization, validation, creation, profile/role/active-state changes, password rotation and bootstrap-admin semantics |
| `shared.run_history` | Readiness audit for historical AI runs and step/structured-output backfill completeness |

`shared.ticketing` is the main domain service boundary. Route modules and worker finalization call it rather than constructing most aggregate mutations directly. It also emits Slack integration events inside the surrounding database transaction.

### AI configuration and workspace

| Module | Responsibility |
| --- | --- |
| `shared.agent_specs` | Validates and loads agent manifests/prompts/skills, enforces spec kinds and shared-policy placeholders, defines pipeline versions |
| `shared.routing_registry` | Parses and cross-validates route targets, handlers, specialists, requester-role eligibility, manual-rerun choices, and publish policies |
| `shared.workspace` | Creates/verifies workspace directories, exact contract and skill files, mounts, and the workspace Git repository |

The routing registry is cached in-process. Changes to spec or registry files require process restart (or explicit cache clearing in tests) to become effective.

### Slack integration

| Module | Responsibility |
| --- | --- |
| `shared.integrations` | Builds immutable ticket-event payloads, resolves requester/assignee recipients, records deduplicated event/link/target outbox rows, records suppression decisions |
| `shared.slack_dm` | DB-backed Slack configuration, HKDF/Fernet token encryption, health snapshots, validation, and thin Slack Web API clients |
| `shared.slack_user_sync` | Persists sync requests/state, fetches paginated Slack users, exact normalized-email matching, conflict-safe user ID updates, error classification |

## 4. `worker` package

| Module | Responsibility | Relationship |
| --- | --- | --- |
| `worker.main` | Builds worker identity; starts heartbeat, Slack sync and Slack delivery threads; runs stale sweep, queue claim and AI processing loop | Top-level worker composition root |
| `worker.queue` | Claims oldest pending run with `SKIP LOCKED`; identifies stale running runs; marks active steps failed; creates recovery runs or routes exhausted work to humans | Owns queue/recovery state transitions |
| `worker.triage` | Prepares/fingerprints runs, executes pipeline, checks stale input, maps output to ticket effects, handles failure and deferred requeue | AI application-service layer |
| `worker.pipeline` | Runs router, optional selector, and optional specialist according to registry; supports synthetic router steps for forced reruns | Router and selector always use `worker.step_runner.execute_step`; persistent transport is specialist-only |
| `worker.codex_inputs` | Builds ordered persistent Codex input events, canonical ticket-message bundles with attachment custody metadata, strict unseen deltas, and causal known-input checks | Cursor between ticket content, persistent turns, and active-turn steering |
| `worker.persistent_codex` | Owns persistent specialist turn preparation, conversation/session leases, exec/app-server transport selection, active-turn steering receipts, accepted input custody, native IDs, and persistent turn finalization | Specialist-only adapter used by `worker.pipeline` when persistent conversations are enabled |
| `worker.codex_app_server` | Run-scoped `codex app-server --stdio` JSON-RPC client with request correlation, protocol item persistence, thread start/resume, turn start/steer, completion wait, failure classification, and bounded cleanup | Used only by persistent specialist execution behind the app-server transport flag |
| `worker.step_runner` | Materializes attachments, renders prompts/schemas, invokes Codex, persists step state/artifacts, validates output and ownership, snapshots run manifest | Infrastructure adapter for model execution |
| `worker.prompt_renderer` | Injects ticket context, visibility flags, route/specialist catalogs, shared specialist policy, and attachment paths into versioned prompts | Consumes specs and routing registry |
| `worker.output_contracts` | Pydantic models and semantic validation for router, selector, specialist, human handoff, and legacy triage results | Validation boundary between model and domain |
| `worker.publication_policy` | Converts model publication recommendation to an allowed effective mode based on target policy, confidence and risk | Called during successful finalization |
| `worker.ticket_loader` | Loads ticket, requester role, public/internal messages and public attachments as an immutable context | Input adapter for pipeline |
| `worker.run_ownership` | Locks and verifies that the current worker instance still owns a running AI run | Used before every material step/finalization update |
| `worker.artifacts` | Defines run/step artifact paths and writes JSON manifests | Filesystem audit adapter |
| `worker.slack_delivery` | Reloads config, validates Slack auth, recovers stale locks, claims delivery batches, renders DMs, classifies retry/terminal errors, finalizes by claim token | Background outbox consumer |
| `worker.slack_user_sync` | Poll loop around shared Slack directory-sync service | Background synchronization consumer |
| `worker.codex_runner` | Deliberately failing legacy compatibility aliases; only `build_codex_command` remains re-exported | Not used by current production flow |

## 5. Agent specification subsystem

Every spec directory contains:

- `manifest.json`: ID, version, kind, output contract, skill ID, optional model/timeout overrides;
- `prompt.md`: runtime prompt template;
- `skill.md`: skill text copied into the isolated triage workspace.

`agent_specs/_shared/specialist_shared_policy.md` is inserted exactly once into every specialist prompt. `agent_specs/registry.json` connects specs to business routing.

| Spec | Kind | Role |
| --- | --- | --- |
| `router` | router | Chooses an enabled, requester-eligible route target |
| `specialist-selector` | selector | Chooses from eligible specialists for an automatic-selection target |
| `support` | specialist | How-to and low-risk troubleshooting |
| `access-config` | specialist | Access, roles, provisioning and configuration |
| `data-ops` | specialist | Imports, corrections, reconciliation, cleanup and backfills |
| `bug` | specialist | Defect/regression investigation |
| `feature` | specialist | Enhancement and workflow requests |
| `business-analyst` | specialist | Requirements framing and viability analysis |
| `software-architect` | specialist | Repository-grounded design and migration assessment |
| `software-data-engineer` | specialist | Repository-grounded change authoring for internal users only |
| `unknown` | specialist | Historical/ambiguous compatibility specialist |

The normal pipeline always runs a router. Direct targets run their fixed specialist as step 2. The human-assist `manual_review` target runs the selector as step 2 and selected specialist as step 3. A forced manual rerun records a synthetic successful router step, then directly runs the selected specialist. Persistent Codex conversations and the app-server transport do not replace router or selector execution.

## 6. Scripts and lifecycle tooling

| Script | Role |
| --- | --- |
| `run_web.py` | Web readiness and Uvicorn entry point; `--check` exercises health and readiness |
| `run_worker.py` | Worker readiness and entry point; can create missing workspace contract files on normal startup |
| `start_all.sh` | Starts worker in background and web in foreground for a single-service deployment |
| `bootstrap_workspace.py` | Creates workspace contract/skills/Git state and seeds system-state defaults |
| `preflight_setup.py` | Validates executable, paths and DB; optionally creates directories and provisions local PostgreSQL |
| `backfill_ai_run_steps.py` | Converts historical terminal runs to the current pipeline/step representation |
| `create_admin.py`, `create_user.py` | Local account bootstrap/provisioning |
| `set_password.py`, `deactivate_user.py` | Account maintenance |
| `setup_postgres_local.sh` | Local-only PostgreSQL installation/start/role/database setup |
| `setup_systemd_services.sh` | Installs separate non-root web and worker systemd services |

Schema changes are ordered Alembic revisions from the initial ticket/session model through preauth, human review, step-based agents, route-target migration, manual rerun metadata, recovery ownership, and Slack outbox/DM persistence.

## 7. Superloop modules

| Module/resource | Responsibility |
| --- | --- |
| `superloop/superloop.py` | CLI parsing, layered YAML config, task/run workspace, phase plan validation, Codex start/resume, Git snapshots, pair cycles, decisions log, events, clarification, and resume |
| `superloop/loop_control.py` | Parses canonical `docloop.loop_control/v1` JSON blocks and legacy question/promise tags; detects unchecked criteria |
| `superloop/templates/*_producer.md` | Planner, implementer, and test-author contracts |
| `superloop/templates/*_verifier.md` | Plan verifier, code reviewer, and test auditor contracts |
| `superloop/templates/*_criteria.md` | Initial completion checklists for each pair |
| `superloop/tests/` | Parser, phase-local behavior, Git tracking, lifecycle/event/resume tests |
| `superloop/legacy/` | Previous implementation retained for reference; not imported by the current CLI |

Superloop resolves configuration in this order: built-ins, config beside the Superloop source, config in the target workspace, then CLI flags. Its default pairs are plan/implement/test, default verifier-cycle limit is 15, default mode is persistent Codex threads with Git checkpoints, and current source default model is `gpt-5.4`.

## 8. Tests

The repository contains roughly 471 named test functions. The AutoSac suite covers auth/requester flows, persistence, operations, AI worker behavior, routing registry, Slack foundation/emission/delivery/sync, upload handling, i18n, and validation hardening. Superloop tests cover loop-control parsing, phase-local artifacts, Git tracking, events, resume and observability.

Tests use pytest and extensive fakes/monkeypatching for database/session and external-process/network boundaries. The root `requirements.txt` includes both runtime and test dependencies; there is no separate package metadata or dependency lock file.
