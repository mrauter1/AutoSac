# AutoSac architecture report

This report documents the implemented architecture of the current working tree. It is based on the source code, configuration, migrations, templates, tests, and deployment scripts in the repository; planning documents are treated as context unless explicitly marked implementation-ready and reflected in source.

For a visual, searchable, and interactive entry point, open the dependency-free [Architecture Atlas](index.html) directly in a browser.

The standalone [Ops ticket layout options](ops-ticket-layout-options.html) is a non-runtime comparison artifact for four bounded conversation layouts with a collapsible inspector on either side. Option A is marked as the selected production treatment.

The implementation-ready [ticket list and queue redesign plan](ticket-list-and-queue-redesign-plan.md) defines the compact requester/Ops list language, visible filter hierarchy, deterministic ordering contract, URL continuity, responsive behavior, and regression boundaries for the next UI phase.

## Report map

| Document | Contents |
| --- | --- |
| [System overview](system-overview.md) | Scope, architectural style, runtime containers, boundaries, technology choices, and major relationships |
| [Module catalog](module-catalog.md) | Responsibilities and dependencies of every production code area, agent specifications, scripts, tests, and auxiliary files |
| [Data model](data-model.md) | PostgreSQL entities, foreign-key relationships, invariants, state machines, and filesystem persistence |
| [Runtime flows](runtime-flows.md) | Authentication, ticket lifecycle, AI pipeline, requeue/recovery, Slack event delivery, uploads, and Superloop execution |
| [Deployment and operations](deployment-and-operations.md) | Configuration, startup, readiness, observability, security controls, failure handling, deployment shapes, and testing |
| [Source traceability](source-traceability.md) | Architectural claims mapped to their primary implementation files and known documentation caveats |

## Active implementation notes

Persistent Codex conversation support and app-server active-turn steering are implemented behind default-off rollout flags. These documents capture the approved scope and the implementation constraints that the current code follows:

- [Persistent Codex conversation plan](persistent-codex-conversation-plan.md) — one append-only Codex conversation per ticket, retained turn history, per-turn structured output, publication metadata, conversation UI, migration, and rollout plan.
- [Codex active-turn steering plan](codex-active-turn-steering-plan.md) — run-scoped `codex app-server --stdio` specialist transport, strict unseen-content steering, delivery receipts, completion/publication fencing, recovery, rollout gates, and unchanged router/selector paths.

Standalone Mermaid sources are in [`diagrams/`](diagrams/):

- [`system-context.mmd`](diagrams/system-context.mmd)
- [`runtime-containers.mmd`](diagrams/runtime-containers.mmd)
- [`ai-pipeline.mmd`](diagrams/ai-pipeline.mmd)
- [`data-model.mmd`](diagrams/data-model.mmd)
- [`slack-delivery.mmd`](diagrams/slack-delivery.mmd)
- [`superloop.mmd`](diagrams/superloop.mmd)

## Executive summary

AutoSac is a server-rendered internal ticket-triage application. A FastAPI process handles authentication, requester and operator workflows, file uploads, and synchronous database mutations. A separate worker process uses PostgreSQL as a durable queue, invokes the Codex CLI in a read-only analysis workspace, validates structured model output, and either publishes a response, creates a human-review draft, or routes the ticket to the operations queue. The same worker process also runs Slack user-sync and Slack DM delivery threads.

PostgreSQL is the system of record for users, sessions, tickets, messages, status history, AI runs and steps, drafts, integration outbox records, Slack settings, and operational state. The filesystem is a second durable store for uploaded attachments, the read-only Codex workspace, and per-run audit artifacts. The web and worker processes must share both the database and workspace configuration.

The repository also contains Superloop, a standalone development-orchestration CLI. Superloop drives producer/verifier Codex loops over a target Git repository and persists its own `.superloop/` task state. It shares no imports, database tables, or runtime calls with the AutoSac application.

## Architectural characteristics

- Modular monolith split into two AutoSac processes: web and worker.
- Database-backed asynchronous work queues using row locks and `SKIP LOCKED`; no external broker.
- Server-rendered HTML with Jinja2 and HTMX fragments; no separate SPA or public JSON API.
- Registry-driven AI routing and publication policy, with versioned prompt/skill specifications.
- Read-only Codex execution with web search disabled and schema-constrained JSON output. Router and selector remain on ephemeral `codex exec`; persistent specialists can use either the legacy exec transport or the default-off run-scoped app-server transport.
- Transactional ticket mutations coupled to an outbox-style integration event model.
- Defense-in-depth around auth tokens, CSRF, role checks, path validation, output validation, stale-lock recovery, and worker ownership.
- PostgreSQL-specific persistence through JSONB, UUID, partial indexes, and sequences.

## Top-level boundaries

```text
Browser
  -> FastAPI/Jinja web process
       -> PostgreSQL
       -> attachment store

Worker process
  -> PostgreSQL AI-run queue
  -> Codex CLI in triage workspace
       -> router/selector ephemeral exec
       -> specialist persistent exec or app-server stdio
  -> run artifact store
  -> Slack Web API

Superloop CLI (independent)
  -> Codex CLI
  -> target Git repository
  -> .superloop task/run files
```
