# PRD — Slack Implementation Refactor for AutoSac

Document status: Draft  
Audience: implementation agent, reviewer, system owner

Normative terms:
- MUST = mandatory
- MUST NOT = prohibited
- SHOULD = recommended
- MAY = optional

## 1. Purpose

This document defines the implementation refactor for AutoSac Phase 1 Slack outbound notifications.

The functional Slack contract is already defined in [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md). That document remains the canonical source of truth for:

- emitted event types, dedupe keys, and payload fields
- routing outcomes and suppression behavior
- Slack message content and sanitization rules
- retry, stale-lock recovery, dead-letter, and at-least-once external delivery semantics
- observability, rollout posture, and minimum functional regression coverage

This refactor PRD changes internal design only where needed to make the implementation more explicit, more maintainable, and safer to evolve. Unless this document explicitly supersedes an internal mechanism, the behavior contract in [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) remains normative.

## 1.1 Pre-Launch Assumption

Phase 1 Slack outbound notifications have not been rolled out in any environment that requires preservation of pre-refactor Slack integration rows or support for mixed pre-refactor and refactor Slack code running concurrently.

This document preserves the functional behavior defined in [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) for the refactored implementation, but it does not require backward compatibility with pre-refactor Slack payload metadata, pre-refactor integration rows, or legacy worker claim/finalization state.

## 2. Refactor Goals

The refactor MUST preserve all externally observable and operational Phase 1 behavior defined in [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) for the refactored implementation while delivering these internal improvements:

1. Replace hidden Slack settings lookup through `Session.info["settings"]` with an explicit runtime boundary.
2. Move preserved routing metadata out of `integration_events.payload_json` into a first-class persisted model.
3. Replace delivery ownership checks that depend on `locked_by` plus `attempt_count` with a single per-claim token.
4. Collapse delivery result handling into one explicit workflow with one finalization boundary for post-claim row mutation.

The refactor SHOULD reduce conditional duplication, make correctness easier to audit, and keep the implementation aligned with current AutoSac SQLAlchemy and worker-process patterns.

## 3. Non-Goals

This refactor does not:

- add new Slack product features, event types, or routing modes
- change Slack message wording, business rules, or user-facing ticket behavior
- change the PostgreSQL-backed event, retry, or dead-letter semantics defined in the Phase 1 Slack PRD
- preserve backward compatibility with unreleased pre-refactor Slack-specific persistence, payload metadata, or mixed-version rollout behavior
- introduce a new dependency-injection framework or a new background-job system
- rework unrelated AI-run worker architecture beyond the boundaries needed to isolate Slack delivery

## 4. Current Problems To Remove

### 4.1 Hidden Settings Boundary

Current emission helpers can discover `Settings` indirectly from `Session.info["settings"]`. That makes the Slack contract depend on ambient session state instead of explicit call-site inputs, and it allows accidental silent no-op behavior when the session was not prepared correctly.

### 4.2 Routing Metadata Mixed Into Business Payload

Current duplicate-preservation behavior stores internal routing metadata under a private payload key inside `integration_events.payload_json`. That couples operator-only routing state to the immutable business event snapshot and makes payload readers responsible for ignoring implementation metadata that is not part of the Slack payload contract.

### 4.3 Ownership Proven By Composite State

Current delivery finalization reloads a `processing` row by combining `id`, `locked_by`, and `attempt_count`. That works, but the ownership proof is indirect. It makes the code reason about both "who claimed this row" and "which attempt number was current" to prove one logical fact: whether the worker still owns this claim.

### 4.4 Finalization Logic Split Across Multiple Paths

Current success, retry, and dead-letter paths each reload and mutate the target row separately. The field writes are similar but spread across multiple functions, which increases the chance of drift when new delivery outcomes or recovery cases are introduced.

## 5. Preserved Contracts

The following contracts from [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) remain unchanged and are not restated here except where this refactor needs a new internal mechanism to satisfy them:

- Sections 3, 6, 7, 8, 9, 10, 11, 12, and 13 remain behaviorally canonical.
- The event payload shape visible in `integration_events.payload_json` remains exactly the Phase 1 payload contract; internal refactor metadata MUST NOT be added to it going forward.
- PostgreSQL remains authoritative for event storage and delivery state.
- External Slack posting remains at-least-once, with one rare duplicate-post window accepted when Slack may have received a webhook before the row was durably marked `sent`.
- Because Phase 1 Slack has not been rolled out yet, backward compatibility with pre-refactor Slack-specific integration state is not part of the preserved contract.

This document supersedes the implementation mechanism, not the business behavior, for:

- how settings are provided to emission and delivery code
- where routing snapshots are stored
- how claim ownership is proven during finalization
- how the worker translates an attempt outcome into one row-state transition

## 6. Target Architecture

The refactored implementation MUST have four explicit responsibilities:

1. `SlackRuntimeContext`
   - immutable runtime dependency bundle
   - contains the resolved `Settings`
   - contains a clock function used for timestamp writes
   - contains logger access or logger adapters if needed

2. `SlackEventRecorder`
   - builds Phase 1 payloads and link rows
   - resolves the emission-time routing decision from `SlackRuntimeContext.settings`
   - persists the event row, link rows, first-class routing snapshot, and optional target row inside the ticket-mutation transaction
   - handles duplicate reuse without repairing or rewriting prior target-row state

3. `SlackDeliveryRepository`
   - performs stale-lock recovery
   - claims due target rows
   - returns immutable claim handles for delivery work
   - finalizes a claim by applying exactly one row-state transition based on a typed delivery outcome

4. `SlackDeliveryExecutor`
   - resolves current send-time target configuration
   - renders Slack text from `payload_json`
   - performs the webhook call
   - classifies the attempt into a typed outcome consumed by the repository finalization step, including retry-exhaustion conversion before finalization

No part of Slack emission or delivery may depend on hidden values stored in the SQLAlchemy session object.

## 7. Explicit Runtime Boundary

### 7.1 Context Contract

Slack emission and delivery entrypoints MUST accept an explicit `SlackRuntimeContext` or an equivalent explicit parameter bundle. The context MUST at minimum provide:

- `settings`
- `now()` or equivalent clock access for persisted timestamps
- any logging handle needed by the emission or worker code

The context MAY be a frozen dataclass or another immutable plain object. A framework-wide injection system is not required.

### 7.2 Call-Site Rules

- Ticket-mutation code that records Slack events MUST pass the runtime context explicitly.
- Worker code that runs Slack delivery cycles MUST pass the runtime context explicitly.
- Slack helpers MUST NOT read `Session.info["settings"]`.
- Slack helpers MUST NOT silently skip event recording because settings were absent from a DB session.
- Missing runtime context at a Slack call site MUST be treated as a programmer error before any Slack integration row is written, not as a valid suppression mode.

This change is internal only. The valid suppression modes remain the routing outcomes already defined in the Phase 1 Slack PRD.

## 8. First-Class Routing Snapshot Persistence

### 8.1 New Immutable Event Columns

`integration_events` MUST gain these nullable columns:

- `routing_result text null`
- `routing_target_name text null`
- `routing_config_error_code text null`
- `routing_config_error_summary text null`

These columns capture the emission-time routing snapshot for the event. They replace the legacy private payload key previously used to preserve routing behavior for duplicate handling.

### 8.2 Column Semantics

- `routing_result` uses the same value set as the Phase 1 routing outcomes:
  - `created`
  - `suppressed_slack_disabled`
  - `suppressed_invalid_config`
  - `suppressed_notify_disabled`
  - `suppressed_target_disabled`
- `routing_target_name` MUST be non-null only when `routing_result` is `created` or `suppressed_target_disabled`.
- `routing_config_error_code` and `routing_config_error_summary` MUST be non-null only when `routing_result` is `suppressed_invalid_config`.
- For new events inserted by the refactored implementation, these routing columns are immutable after insert.
- `payload_json` MUST contain only the Phase 1 payload contract. Newly inserted rows MUST NOT store `_integration_routing` or any equivalent implementation-only metadata inside `payload_json`.

### 8.3 Duplicate Handling Contract

For rows written by the refactored implementation, duplicate handling MUST obey the following contract:

- duplicate reuse MUST preserve the existing event row and target-row state exactly as required by the Phase 1 Slack PRD
- duplicate handling MUST read routing state from the persisted routing columns
- if the reused event has one or more target rows, duplicate handling MUST report the duplicate as `created` for observability purposes and MUST use the stored target row's `target_name`
- if the reused event has zero target rows and the stored routing snapshot is one of the non-`created` suppression outcomes, duplicate handling MUST report that stored suppression outcome
- if the reused event has zero target rows and the stored routing snapshot is absent or says `created`, duplicate handling MUST treat the observable duplicate outcome as `suppressed_notify_disabled`

That last rule is intentionally conservative. It preserves the "never repair or create a second target row" contract without fabricating a successful created-target outcome for an event that currently has no target row.

## 9. Claim Token Ownership Model

### 9.1 New Target Column

`integration_event_targets` MUST gain:

- `claim_token uuid null`

`claim_token` is internal ownership state. It is not part of the operator-facing business contract and does not replace `locked_by` for observability.

### 9.2 Claim Semantics

On every successful claim of an eligible `pending` or `failed` row, the worker MUST:

- set `delivery_status = 'processing'`
- increment `attempt_count` exactly once
- set `locked_at`
- set `locked_by`
- generate and persist a fresh `claim_token`

The claim handle returned to delivery code MUST include:

- `target_id`
- `event_id`
- `event_type`
- `target_name`
- `attempt_count`
- `locked_by`
- `claim_token`
- `payload_json`

### 9.3 Finalization Ownership Proof

Any post-claim mutation of a `processing` row MUST prove ownership using this predicate:

- `id = claimed target id`
- `delivery_status = 'processing'`
- `claim_token = claimed claim_token`

No legacy ownership fallback is required. This PRD assumes refactored workers operate only on refactor-era claims.

### 9.4 Clearing Claim Ownership

Any transition out of `processing`, including stale-lock recovery, MUST clear:

- `claim_token`
- `locked_at`
- `locked_by`

Stale-lock recovery MUST preserve `attempt_count`, exactly as required by the Phase 1 Slack PRD.

## 10. Explicit Delivery Outcome Model

### 10.1 Delivery Outcome Shape

The delivery executor MUST classify each claim into one typed delivery outcome before any final row write occurs. A conforming delivery-outcome model MUST distinguish at least:

- `sent`
- `retryable_failure`
- `dead_letter_terminal`

Each delivery outcome MUST carry the exact data needed to perform the row update without re-deriving business logic later. A conforming outcome model MUST include:

- `kind`: one of `sent`, `retryable_failure`, or `dead_letter_terminal`
- `last_error`: null for `sent`; required sanitized summary for non-`sent`
- `http_status`: integer only when the outcome came from an HTTP response; otherwise null or omitted
- `failure_class`: required stable classifier for non-`sent`; omitted for `sent`
- `next_attempt_at`: required only when `kind = retryable_failure`
- `terminal_reason`: required only when `kind = dead_letter_terminal`, with value `terminal_failure` or `retry_exhausted`

The executor, or a helper owned by the executor, MUST compute the outcome using the claim handle's post-claim `attempt_count` and the same `SlackRuntimeContext.settings` used for that delivery attempt. The outcome is authoritative. Repository finalization MUST NOT recompute retry backoff, re-read `SLACK_DELIVERY_MAX_ATTEMPTS`, or convert one outcome kind into another.

Finalization MUST additionally distinguish whether the worker still owns the row. If the row no longer matches the required ownership predicate, finalization MUST return or log `ownership_lost` and MUST leave the row unchanged.

### 10.2 Classification Rules

The executor MUST preserve the classification rules from the Phase 1 Slack PRD:

- 2xx MUST produce `sent`
- missing or disabled target config at send time MUST produce `dead_letter_terminal` with `terminal_reason = terminal_failure` and MUST make no HTTP request
- render-time payload validation failure MUST produce `dead_letter_terminal` with `terminal_reason = terminal_failure`
- HTTP 3xx and non-retryable 4xx MUST produce `dead_letter_terminal` with `terminal_reason = terminal_failure`
- transport errors, ambiguous timeouts, HTTP 408, HTTP 429, and HTTP 5xx are retryable classes
- when a retryable class occurs and the claim handle's post-claim `attempt_count` is less than `SLACK_DELIVERY_MAX_ATTEMPTS`, the executor MUST produce `retryable_failure` and MUST compute `next_attempt_at` with the unchanged Phase 1 backoff formula
- when a retryable class occurs and the claim handle's post-claim `attempt_count` is greater than or equal to `SLACK_DELIVERY_MAX_ATTEMPTS`, the executor MUST instead produce `dead_letter_terminal` with `terminal_reason = retry_exhausted` and MUST NOT provide `next_attempt_at`

### 10.3 Single Finalization Boundary

`SlackDeliveryRepository` MUST expose one finalization operation that accepts:

- the claim handle
- the typed delivery outcome

That operation is the only place allowed to mutate a claimed row after claim time. Success, retry, dead-letter, and stale-lock recovery MUST all use canonical field-write sets defined in one place.

For claimed-row finalization, the repository MUST choose the write set solely from the ownership check plus the supplied outcome fields. It MUST NOT reclassify retry exhaustion, recompute `next_attempt_at`, or reinterpret HTTP or failure classes.

The implementation MAY keep helper functions internally, but the row-state contract MUST have one canonical owner so future edits cannot drift across multiple copies of the same state-transition logic.

## 11. Delivery Workflow

The worker delivery cycle MUST follow this order:

1. Resolve global Slack suppression from current settings.
2. If globally suppressed:
   - obey the unchanged suppression rules from the Phase 1 Slack PRD
   - emit the required invalid-config suppression log when applicable
   - perform no claim, send, or stale-lock mutation work
3. If not suppressed:
   - perform stale-lock recovery
   - claim due rows in batches
   - for each claim, build a typed delivery outcome
   - finalize the claim through the single finalization boundary

For one claimed row, the executor workflow MUST be:

1. load current target config by `target_name`
2. if missing or disabled, produce terminal pre-send outcome
3. render Slack text from `payload_json`
4. execute the webhook request with the Phase 1 timeout and redirect rules
5. classify the HTTP result or exception
6. finalize exactly once

No step after claim time may re-read ticket, message, or status-history tables to rebuild event content.

## 12. Migration and Pre-Launch Deployment

### 12.1 Schema Changes

The refactor migration MUST:

- add the routing snapshot columns to `integration_events`
- add nullable `claim_token` to `integration_event_targets`

The initial migration MUST NOT rename or drop existing columns such as `locked_by`, `locked_at`, `attempt_count`, or `payload_json`.

### 12.2 Pre-Launch Compatibility Scope

Because Phase 1 Slack has not been rolled out yet, backward compatibility with pre-refactor Slack-specific integration state is not required.

- refactored event recording MUST write the first-class routing columns for all new events
- refactored event recording MUST NOT write legacy `_integration_routing` metadata into `payload_json`
- refactored delivery claims MUST always write `claim_token`
- refactored code does not need to read legacy payload routing metadata
- refactored code does not need to support legacy `processing` rows with `claim_token IS NULL`

Any environment that contains pre-refactor Phase 1 Slack integration rows MUST treat them as disposable pre-launch data. Before enabling Slack in that environment, operators MUST remove those rows or otherwise reset Slack integration state so only refactor-era rows remain.

This assumption does not authorize changing ticket rows, message rows, status-history rows, or AI-run rows. It applies only to Slack-specific integration state.

### 12.3 Deployment Assumption

Deployment MAY assume `SLACK_ENABLED=false` during schema upgrade and application/worker deployment.

- request-path and worker code SHOULD be deployed together as one refactor-aware versioned unit
- no mixed-version compatibility bridge for request-path duplicate handling or worker claim finalization is required
- once the refactored schema and code are in place, and any disposable pre-launch Slack rows are cleared if needed, Slack MAY be enabled and verified using the normal Phase 1 dark-launch posture

## 13. Observability Impact

The observability contract in the Phase 1 Slack PRD remains canonical. This refactor adds these implementation requirements:

- emission logs MUST source `routing_result`, `target_name`, and config-error details from the first-class routing snapshot, not from payload-embedded metadata
- ownership-lost warning logs SHOULD include `claim_token` when the row was claimed by refactor code
- delivery-runtime logs MAY include `claim_token` as additional debugging context, but they MUST continue to include the Phase 1 required fields

`locked_by` remains part of the operator-facing log contract even though correctness no longer depends on it.

## 14. Rollout Plan

The refactor MUST be delivered in phases:

1. keep `SLACK_ENABLED=false`
2. ship the schema expansion and deploy refactored request-path and worker code together as one versioned unit
3. if the environment contains any pre-refactor Phase 1 Slack integration rows, clear that disposable pre-launch Slack state before enabling Slack
4. verify new event recording and duplicate reuse while Slack remains disabled, then verify claim/finalization and delivery behavior with the refactored implementation in a non-production or otherwise controlled pre-launch check
5. enable the desired Slack target and notify flags for the target environment
6. monitor for:
   - duplicate event reuse with zero target rows
   - ownership-lost warnings
   - unexpected stale-lock recovery volume
   - dead-letter and retry counts staying within existing expectations

Rollback posture remains config-first:

- `SLACK_ENABLED=false` still stops claim, send, and stale-lock mutation as already defined in the Phase 1 Slack PRD
- redeploying or reverting to another refactor-aware worker or application build MUST NOT require dropping the new columns
- this PRD does not require or define rollback compatibility with pre-refactor Slack code or pre-refactor Slack integration state
- if a pre-launch environment must temporarily return to a pre-refactor build, operators MUST keep Slack disabled and treat any refactor-era or pre-refactor Slack integration rows as disposable pre-launch data

No historical Slack backfill is part of this refactor.

## 15. Required Test Additions

All minimum coverage from [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) still applies. In addition, the refactor MUST add or update tests covering at least:

- event-recording entrypoints require explicit runtime context and no longer read `Session.info["settings"]`
- newly inserted `integration_events` rows populate the routing snapshot columns and do not write `_integration_routing` into `payload_json`
- duplicate handling on a reused event with one target row reports `created` without creating or mutating target rows
- duplicate handling on a reused zero-target event uses the persisted non-`created` routing snapshot when available
- duplicate handling on a reused zero-target event with missing or stale `created` routing snapshot falls back to `suppressed_notify_disabled` and still creates no repair row
- refactor-created zero-target events still behave correctly during duplicate reuse from first-class routing columns alone
- claiming a target row writes a fresh `claim_token`
- success, retry, dead-letter, and stale-lock recovery all clear `claim_token`
- finalization by the wrong `claim_token` produces ownership-lost behavior and leaves the row unchanged
- a retryable transport or HTTP failure below `SLACK_DELIVERY_MAX_ATTEMPTS` produces `retryable_failure`, carries the Phase 1 backoff timestamp, and finalizes to `failed`
- the same retryable failure at post-claim `attempt_count >= SLACK_DELIVERY_MAX_ATTEMPTS` produces `dead_letter_terminal` with `terminal_reason = retry_exhausted` before finalization and finalizes directly to `dead_letter`
- repository finalization does not recompute retry exhaustion or `next_attempt_at`; it only applies the supplied outcome write set or reports ownership lost
- the single finalization boundary applies the same field-write sets previously required for `sent`, `failed`, and `dead_letter`

## 16. Acceptance Criteria

This refactor is complete when all of the following are true:

1. Every Phase 1 Slack behavior test still passes unchanged unless the test is being updated only to reflect the internal storage move from payload metadata to first-class routing columns and the intentional removal of pre-launch-only legacy compatibility expectations.
2. No Slack emission or delivery code path depends on `Session.info["settings"]`.
3. New events persist routing snapshots in first-class columns and no longer persist private routing metadata inside `payload_json`.
4. New delivery claims use `claim_token`, and post-claim row mutation is centralized behind one finalization boundary.
5. Pre-launch deployment assumptions are explicit: upgrades occur with `SLACK_ENABLED=false`, refactored request-path and worker code are deployed together, and any pre-refactor Slack integration rows are treated as disposable pre-launch data rather than a compatibility target.
6. Retry exhaustion is decided before repository finalization, and finalization only applies the canonical write set for the supplied outcome.
