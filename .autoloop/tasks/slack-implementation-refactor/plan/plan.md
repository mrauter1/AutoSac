# Slack Implementation Refactor Plan

## Scope Considered

- Authoritative source: the run request snapshot and `tasks/slack_implementation_refactor_PRD.md`; the raw phase log contains no later clarifications.
- Baseline code inspected: `shared/integrations.py`, `shared/ticketing.py`, `worker/slack_delivery.py`, `shared/models.py`, `shared/db.py`, `shared/migrations/versions/20260410_0010_slack_integration_foundation.py`, and the Slack-focused tests.
- Out of scope unless the PRD explicitly requires it: new Slack product features, mixed-version runtime bridges, unrelated AI worker redesign, or any reset of non-Slack domain data.

## Baseline Delta Summary

- `shared/integrations.py` still resolves `Settings` from either an explicit argument or `Session.info["settings"]`, and missing settings currently let emission helpers return `None` instead of failing fast.
- Routing preservation is still stored under `_integration_routing` inside `IntegrationEvent.payload_json`, and zero-target duplicate reuse depends on that payload metadata.
- `shared/ticketing.py` only passes settings explicitly on some Slack-emitting paths. `record_status_change`, `publish_ai_public_reply`, `add_ops_public_reply`, and `publish_ai_draft_for_ops` currently rely on ambient session settings through downstream helpers.
- `worker/slack_delivery.py` mixes stale-lock recovery, claim, send-time classification, retry-budget conversion, and post-claim row mutation in one module, but with three separate finalization helpers and ownership proven by `(id, locked_by, attempt_count)`.
- `ClaimedDeliveryTarget` does not include a claim token, `IntegrationEvent` lacks first-class routing snapshot fields, `IntegrationEventTarget` lacks `claim_token`, and the tests currently encode the payload-metadata and split-finalization design.

## Locked Implementation Shape

### Runtime boundary

- Add a frozen `SlackRuntimeContext` in the existing Slack integration layer, with `settings`, `now()`, and the logging handles or adapters needed by emission and delivery.
- Remove `resolve_integration_settings()` and all Slack reads from `Session.info["settings"]`.
- Require explicit `slack_runtime` or equivalent context on Slack entrypoints, then thread it through the existing ticketing/request/worker call graph instead of introducing a new DI framework.
- Signature changes must reach the ticketing helpers that can emit Slack state:
  - `record_status_change`
  - `publish_ai_public_reply`
  - `add_ops_public_reply`
  - `publish_ai_draft_for_ops`
  - the direct `record_ticket_*` emission helpers
- Request and worker entrypoints should construct the context from the already-available `Settings` and pass it down once per transaction or delivery cycle.

### Persistence and duplicate handling

- Add one additive Alembic migration after `20260410_0010` and update `shared/models.py` for:
  - `integration_events.routing_result`
  - `integration_events.routing_target_name`
  - `integration_events.routing_config_error_code`
  - `integration_events.routing_config_error_summary`
  - `integration_event_targets.claim_token`
- Keep emission ownership in `shared/integrations.py`; introduce small typed helpers there rather than a new Slack package.
- New event inserts must write routing snapshot columns and keep `payload_json` limited to the Phase 1 business payload contract.
- Duplicate reuse must read only the stored routing columns plus existing target rows:
  - existing target row present => observable duplicate result is `created` with stored `target_name`
  - zero targets plus stored non-`created` suppression => reuse that stored suppression
  - zero targets plus missing or stale `created` snapshot => degrade to `suppressed_notify_disabled`
- The refactor must not repair, backfill, or create target rows during duplicate reuse.

### Delivery ownership and finalization

- Keep delivery logic centered in `worker/slack_delivery.py`, but split it into explicit typed responsibilities inside that module or a minimal companion helper:
  - repository helpers for stale-lock recovery, claiming, and finalization
  - executor helpers for render/send/classify
  - typed `ClaimedDeliveryTarget` and `DeliveryOutcome`
- Claiming a row must set `delivery_status='processing'`, increment `attempt_count` once, stamp `locked_at`, `locked_by`, and a fresh `claim_token`, then return a claim handle that includes the claim token.
- Finalization ownership proof becomes:
  - `id = target_id`
  - `delivery_status = 'processing'`
  - `claim_token = claimed claim_token`
- Success, retry, dead-letter, and stale-lock recovery must all clear `claim_token`, `locked_at`, and `locked_by`. Stale-lock recovery must keep `attempt_count`.
- Retry exhaustion must be decided before finalization and encoded in the typed outcome. Repository finalization only applies the supplied write set or reports ownership lost.

### Module boundaries and change scope

- Preserve the current module ownership:
  - `shared/integrations.py` owns payload building, routing, event persistence, duplicate reuse, and emission logging.
  - `shared/ticketing.py` owns ticket-domain transactions and becomes the explicit propagation path for `SlackRuntimeContext`.
  - `worker/slack_delivery.py` owns delivery suppression, stale-lock recovery, claiming, send-time execution, and finalization.
- Avoid a broad package split. Add only the minimal dataclasses/helpers needed to make the refactor explicit and testable.

## Milestones

### Milestone 1: Schema and emission/runtime boundary refactor

- Add the routing snapshot columns and `claim_token` in a new additive migration and update SQLAlchemy models.
- Introduce `SlackRuntimeContext` and remove `Session.info["settings"]` access from Slack code.
- Refactor `shared/integrations.py` so new inserts populate first-class routing fields and never write `_integration_routing`.
- Update duplicate reuse to consult stored routing columns and existing target rows only.
- Thread explicit Slack runtime through `shared/ticketing.py` and the request/worker callers that can emit Slack events.

Exit criteria:

- No Slack emission path depends on `Session.info["settings"]`.
- New events populate the routing snapshot columns and keep `payload_json` free of implementation metadata.
- Zero-target duplicate reuse follows the PRD fallback rules without creating or mutating target rows.
- Ticket mutation entrypoints that can emit Slack compile and run with explicit runtime propagation.

### Milestone 2: Delivery claim token and single finalization boundary

- Extend claim handles and repository queries to use `claim_token`.
- Refactor stale-lock recovery, claiming, and finalization into one canonical owner for post-claim row mutation.
- Introduce typed delivery outcomes covering `sent`, `retryable_failure`, and `dead_letter_terminal`.
- Move retry-budget conversion into the executor/classification path so finalization never recomputes retry exhaustion or `next_attempt_at`.
- Preserve current delivery ordering, backoff formula, suppression behavior, render contract, timeout rules, and logging contract while adding claim-token context where allowed.

Exit criteria:

- Claiming writes a fresh `claim_token`, and success/retry/dead-letter/stale-lock paths all clear it.
- Finalization uses only `(id, processing, claim_token)` to prove ownership and leaves rows unchanged on ownership loss.
- Retryable failures below the limit finalize to `failed` with executor-supplied `next_attempt_at`; retryable failures at or above the limit finalize directly to `dead_letter`.
- `worker/slack_delivery.py` has one canonical finalization boundary for claimed-row writes.

### Milestone 3: Regression completion and rollout verification

- Update Slack emission, delivery, migration, and persistence tests to the refactored storage and ownership model.
- Extend migration coverage to assert the new additive columns exist and the old Slack foundation migration remains intact.
- Update any rollout-facing docs or check scripts only where they need to reflect the explicit pre-launch assumptions for disposable Slack rows and config-first rollback.
- Run the targeted Slack regression set and resolve any drift introduced by the signature changes or state-transition consolidation.

Exit criteria:

- The updated test suite covers all PRD-required additions, including explicit runtime context, first-class routing persistence, claim-token ownership loss, and repository-applied write sets.
- There are no remaining code or test references that require `_integration_routing` or legacy ownership checks.
- Rollout and rollback assumptions stay explicit: deploy code and worker together with `SLACK_ENABLED=false`, treat pre-refactor Slack rows as disposable pre-launch state, and keep rollback config-first.

## Compatibility, Rollout, and Rollback

- The refactor keeps the existing additive schema posture: add columns only, do not rename or drop existing Slack columns in this change.
- Implementation phases are coding order, not separate rollout checkpoints. Deployment still assumes a single refactor-aware application/worker version plus `SLACK_ENABLED=false` during migration and rollout validation.
- No compatibility bridge is planned for:
  - payload `_integration_routing`
  - legacy `processing` rows with `claim_token IS NULL`
  - mixed pre-refactor and refactor Slack workers
- If a pre-launch environment already contains old Slack integration rows, operators must clear only Slack integration state before enabling Slack. Ticket, message, status-history, and AI-run tables are out of scope for cleanup.
- Rollback remains config-first. After the additive migration lands, operational rollback should disable Slack and revert code, not attempt to restore legacy runtime compatibility.

## Regression Controls

- Preserve the Phase 1 payload contract by asserting exact payload keys and render behavior separately from routing snapshot persistence.
- Centralize post-claim write sets so the `sent`, `failed`, and `dead_letter` transitions cannot drift across helper copies.
- Keep suppression semantics unchanged: when Slack is globally disabled or invalid, claim, send, and stale-lock recovery must all be skipped.
- Preserve claim ordering, batch limits, retry delay formula, timeout behavior, and at-least-once external delivery semantics.
- Keep duplicate reuse read-only with respect to `integration_event_targets`.

## Test and Verification Plan

- Emission tests:
  - explicit runtime context is required
  - no `_integration_routing` writes
  - new routing columns are populated correctly
  - duplicate reuse cases for existing target rows, stored suppressions, and zero-target `created` fallback
- Delivery tests:
  - claim writes a fresh `claim_token`
  - stale-lock recovery clears claim state but preserves `attempt_count`
  - wrong claim token logs ownership lost and leaves the row unchanged
  - retryable failures below and at retry exhaustion produce the correct typed outcome and final row state
  - repository finalization applies only the supplied outcome fields
- Migration and persistence tests:
  - new migration adds the routing and claim-token columns
  - existing foundation migration assertions still hold
  - any persistence helpers reading/writing the new fields behave as expected
- Targeted validation run:
  - `tests/test_slack_event_emission.py`
  - `tests/test_slack_delivery.py`
  - `tests/test_foundation_persistence.py`
  - any route/worker tests touched by signature propagation

## Risk Register

- R1: A Slack-emitting call site keeps relying on ambient session settings and either silently skips emission or fails late.
  Mitigation: remove the fallback helper entirely, search all `record_ticket_*`, `record_status_change`, and Slack-emitting ticketing call sites, and keep explicit-runtime tests around the affected request/worker paths.
- R2: Duplicate reuse regresses zero-target behavior by consulting current settings instead of stored routing state.
  Mitigation: move duplicate logic behind first-class routing readers and add explicit tests for stored suppression, stored `created` plus zero targets, and existing-target reuse.
- R3: Finalization drift persists because row mutations remain spread across success/retry/dead-letter helpers.
  Mitigation: make repository finalization the only post-claim mutation owner and keep table-driven tests for each outcome kind.
- R4: Claim-token ownership changes weaken observability or stale-lock recovery.
  Mitigation: keep `locked_by` in claim handles and logs, add claim-token context to ownership-lost logs where allowed, and preserve the existing stale-lock recovery cadence and semantics.
- R5: Signature propagation through request and worker entrypoints causes incidental regressions outside Slack behavior.
  Mitigation: confine parameter changes to Slack-emitting helpers and the minimal upstream entrypoints, then rerun the affected route/worker tests alongside the Slack suite.
