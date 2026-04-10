# Phase 1 Slack Outbound Notifications Plan

## Goal
Implement Phase 1 outbound Slack notifications as a PostgreSQL-backed asynchronous projection of ticket facts, while preserving all existing Stage 1 ticket, status-history, unread/view, and AI-run invariants.

## Repo-grounded constraints
- Ticket mutations are centralized in `shared/ticketing.py`; emission hooks should stay there or in worker flows that already call those helpers.
- The worker already has a main AI polling loop plus a heartbeat thread in `worker/main.py`; Slack delivery should be a separate thread with separate `session_scope` usage.
- Safe `FOR UPDATE SKIP LOCKED` claim/recovery patterns already exist in `worker/queue.py`; reuse that concurrency shape.
- Structured JSON logging already flows through `shared/logging.py`; extend it instead of adding a second logging stack.
- Existing unit tests rely heavily on fake sessions and source assertions; keep the Slack seam thin enough that unrelated requester/ops tests do not need broad rewrites.

## Implementation design

### 1. Persistence and configuration foundation
- Add one Alembic migration after `20260408_0009` for:
  - `integration_events`
  - `integration_event_links`
  - `integration_event_targets`
- Extend `shared/models.py` with ORM models plus Phase 1 enum/constant tuples for event types, link entity types, relation kinds, target kind, and delivery statuses.
- Extend `shared/config.py` with Slack env inputs and a structured Slack runtime config helper that exposes:
  - parsed notify flags and scalar tunables
  - named target definitions
  - soft validation state: `is_valid`, `config_error_code`, `config_error_summary`
- Slack-specific misconfiguration must not raise `SettingsError` or fail startup. The PRD requires ticket mutations and worker startup to keep functioning when Slack is disabled or misconfigured; invalid Slack config becomes a suppression state, not an outage.
- Update `.env.example`, `README.md`, and `docs_deployment.md` with the new env surface and rollout guidance.

### 2. In-transaction event emission
- Add one shared integration module (for example `shared/integrations.py`) to own:
  - `ticket_url` normalization from `APP_BASE_URL`
  - `message_preview` normalization/truncation
  - payload builders for `ticket.created`, `ticket.public_message_added`, and `ticket.status_changed`
  - routing outcome resolution (`created`, `suppressed_*`)
  - dedupe-safe event/link/target persistence
  - sanitized emission-log payload assembly
- Keep `shared/ticketing.py` responsible for deciding when a business fact occurs. Keep the integration module responsible for recording the immutable snapshot and optional target row.
- Hook only the existing central mutation helpers:
  - `create_requester_ticket` -> `ticket.created`
  - `add_requester_reply`
  - `add_ops_public_reply`
  - `publish_ai_public_reply`
  - `publish_ai_draft_for_ops`
  - `record_status_change` -> `ticket.status_changed`, skipping the initial `null -> new` row and any no-op transition
- Non-emitting rows remain non-emitting rows: internal-note rows, AI internal-note rows, AI failure-note rows, draft rejection, assignment/view changes, and any action that creates neither an eligible public message nor a distinct non-initial status transition.
- Distinguish row-level suppression from flow-level status events: AI failure, AI internal route-only, and draft-creation flows still emit `ticket.status_changed` through `record_status_change` whenever they commit a distinct non-initial status transition, even though their accompanying internal note or draft rows do not emit `ticket.public_message_added`.
- Duplicate `dedupe_key` handling should reuse the existing event row and leave previously stored integration state untouched rather than trying to repair links or targets from current config.

### 3. Delivery runtime
- Add a worker-only module (for example `worker/slack_delivery.py`) to own:
  - suppression checks from structured Slack config state
  - stale-lock recovery for `integration_event_targets`
  - batched claim/update logic with `FOR UPDATE SKIP LOCKED`
  - payload-only Slack rendering and escaping
  - webhook POST via `httpx` with `Content-Type: application/json`, no redirects, and hard timeout
  - success / retry / dead-letter transitions with sanitized operator-facing errors
- Start a dedicated delivery thread from `worker/main.py` alongside the heartbeat thread. It must not reuse AI-run transactions and must not serialize behind `process_ai_run`.
- Use `settings.worker_poll_seconds` as the delivery poll cadence unless implementation uncovers a repo-proven blocker. The PRD does not add a separate delivery poll env.
- While Slack is globally suppressed (`SLACK_ENABLED=false` or invalid config), the delivery loop must skip claim, send, and stale-lock recovery and only emit suppression logs.

### 4. Logging and testable interfaces
- Reuse `shared.logging`. Emission-path logs may use a dedicated service name such as `integration`; delivery-runtime logs should remain worker logs.
- Keep the following internal seams explicit for unit coverage:
  - emission helper returns enough state for log assertions (`event_id`, `routing_result`, optional `target_name`)
  - config helper exposes stable machine-readable invalid-config codes
  - delivery helpers separate stale recovery, claim, render, send, and row-finalization paths so attempt-count and retry semantics are testable without real HTTP

## Milestones
1. Schema and config foundation
2. In-transaction event emission
3. Async delivery runtime
4. Regression hardening and rollout readiness

## Affected files and ownership
- `shared/models.py`
- `shared/config.py`
- new shared integration module (`shared/integrations.py` or equivalent)
- `shared/ticketing.py`
- new Alembic migration after `20260408_0009`
- `worker/main.py`
- new delivery module under `worker/`
- `.env.example`
- `README.md`
- `docs_deployment.md`
- tests: likely a dedicated Slack integration test module plus targeted additions in `tests/test_ai_worker.py`, `tests/test_foundation_persistence.py`, and `tests/test_hardening_validation.py`

## Compatibility and regression notes
- No requester-visible UI change is required.
- No existing `tickets`, `ticket_messages`, `ticket_status_history`, or `ai_runs` row semantics may change.
- No backfill is part of this rollout.
- Delivery must render from `integration_events.payload_json` only; it must not reload current ticket/message rows during send.
- `run_worker.py --check` and normal app startup must not become Slack-dependent for availability. Invalid Slack config is a suppression case, not a startup blocker.
- Keep the emission hook local to shared mutation helpers instead of spreading Slack writes into route handlers, so duplicate/no-event rules stay consistent across web and worker callers.

## Validation plan
- Migration/source tests assert the new tables, constraints, and indexes.
- Unit tests cover:
  - `ticket_url` normalization
  - `message_preview` normalization and truncation
  - event payload content and author/source rules
  - routing outcomes for disabled, invalid, notify-disabled, target-disabled, and created cases
  - duplicate dedupe reuse without target repair
  - non-emitting note/draft-rejection rows, worker-driven status-only flows, and skipped initial/no-op status transitions
- Worker tests cover:
  - stale-lock recovery
  - claim ordering / `SKIP LOCKED`
  - success, retryable failure, retry exhaustion, terminal dead-letter
  - missing/disabled target handling without outbound HTTP
  - suppression while Slack is disabled or globally invalid
- Logging tests/assertions cover emission `routing_result` separation and invalid-config suppression payload shape.
- Docs/hardening tests verify the new env vars appear in `.env.example` and operator docs.

## Rollout and rollback
- Rollout:
  1. ship migration + emission with `SLACK_ENABLED=false`
  2. verify event/link rows and logs in non-production
  3. enable one named target
  4. enable the desired `SLACK_NOTIFY_*` flags gradually
- Rollback:
  - set `SLACK_ENABLED=false` to stop delivery immediately while preserving stored integration history
  - if code rollback is required, revert worker/thread/config changes without mutating existing ticket data
  - only drop the new integration tables if that data loss is explicitly accepted; the default rollback should keep append-only event history intact

## Risk register
- R1: Internal-content leakage if previews are built from internal rows, raw markdown, or current ticket reloads.
  Mitigation: build payloads only from eligible public rows at mutation time; render only from `payload_json`; escape user-derived text before Slack send.
- R2: Duplicate or repaired target state after retries, config changes, or duplicate emission attempts.
  Mitigation: use deterministic dedupe keys tied to persisted row identity and treat duplicate reuse as read-only for preexisting integration rows.
- R3: Slack misconfiguration accidentally takes down core web/worker startup.
  Mitigation: Slack validation is soft and produces suppression/logging state instead of startup failure.
- R4: Delivery work interferes with AI-run polling or lock recovery.
  Mitigation: separate thread, separate DB sessions, and the same lock discipline already used for AI-run claiming.
- R5: Attempt counts or stale-lock transitions drift from the PRD.
  Mitigation: keep recovery/claim/finalization helpers isolated and cover pre-send terminal failure, retryable timeout, retry exhaustion, and suppression paths with direct tests.
