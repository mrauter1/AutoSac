# PRD — Phase 1 Slack Outbound Notifications for AutoSac

Document status: Draft  
Audience: implementation agent, reviewer, system owner

Normative terms:
- MUST = mandatory
- MUST NOT = prohibited
- SHOULD = recommended
- MAY = optional

## 1. Purpose

This document extends [Autosac_PRD.md](/home/marcelo/code/AutoSac/tasks/Autosac_PRD.md) with Phase 1 outbound Slack notifications only.

The goal is to notify internal Slack recipients about important ticket activity without making Slack part of the source-of-truth workflow. PostgreSQL remains authoritative. Slack is an asynchronous projection of selected ticket events.

## 2. Scope Boundary

Phase 1 Slack scope includes:
- append-only integration event recording for selected ticket facts
- per-target delivery state persisted in PostgreSQL
- asynchronous delivery to one configured Slack webhook target
- retry, dead-letter, and restart-safe delivery behavior
- operational visibility through DB rows and worker logs

Phase 1 Slack scope excludes:
- inbound Slack messages
- slash commands, buttons, threads, or interactive Slack actions
- requester-visible behavior changes
- per-user or per-ticket subscription settings
- retroactive backfill for tickets, messages, or status history that predate the migration
- fan-out to multiple active Slack targets in one environment
- reuse of `ai_runs` as a notification queue

The schema MUST support multiple named targets from day 1, but this PRD routes each event to at most one target in Phase 1.

## 3. Preserved AutoSac Invariants

The Slack integration MUST preserve the existing Stage 1 behavior defined in [Autosac_PRD.md](/home/marcelo/code/AutoSac/tasks/Autosac_PRD.md):

- Ticket creation, requester replies, ops replies, AI publication, and status changes MUST succeed even when Slack is disabled, misconfigured, unreachable, rate-limited, or returning errors.
- `tickets.updated_at`, `ticket_status_history`, unread/view semantics, and the requester/dev-TI visibility boundary MUST remain unchanged.
- Internal notes and any content visible only in `ticket_messages.visibility = internal` MUST NEVER be emitted to Slack.
- The one-active-`ai_run` invariant and the existing AI-run queue semantics MUST remain unchanged.
- Exactly-once event storage and exactly one mutable target row per `(event_id, target_name)` in PostgreSQL MUST be preserved across duplicate emission attempts and worker restarts. Externally visible Slack posting follows the at-least-once contract in Section 9.4.
- The migration MUST NOT alter existing ticket rows, message rows, status-history rows, or AI-run rows.

## 4. Architecture Summary

Phase 1 Slack delivery has three persisted layers:

1. `integration_events`
   Canonical append-only business facts captured inside the same DB transaction as the ticket mutation.
2. `integration_event_links`
   Optional cross-references from an event to related domain rows.
3. `integration_event_targets`
   Mutable delivery state for one named destination per event.

The delivery runtime MUST live in the existing worker process, but it MUST execute independently of the request path and independently of AI-run polling. A dedicated delivery thread is a conforming implementation, but the contract is that Slack delivery remains asynchronous and separately scheduled.

The delivery runtime MUST render Slack messages from `integration_events.payload_json`. It MUST NOT re-read current ticket or message rows to rebuild historical content, because the event payload is the event-time snapshot.

## 5. Persisted Model

### 5.1 `integration_events`

Purpose: canonical immutable record of an integration-relevant business fact.

Columns:
- `id uuid primary key`
- `source_system text not null default 'autosac'`
- `event_type text not null`
  allowed values in Phase 1:
  - `ticket.created`
  - `ticket.public_message_added`
  - `ticket.status_changed`
- `aggregate_type text not null`
  allowed value in Phase 1:
  - `ticket`
- `aggregate_id uuid null`
- `dedupe_key text not null unique`
- `payload_json jsonb not null`
- `created_at timestamptz not null default now()`

Rules:
- In this rollout, every emitted event MUST set `aggregate_type = 'ticket'`.
- In this rollout, every emitted event MUST set `aggregate_id = payload_json.ticket_id`. `aggregate_id` remains nullable only for future event families and MUST NOT be null in Phase 1 Slack delivery.
- `payload_json` MUST contain the full event-time snapshot needed for Slack rendering.
- Rows are append-only. `event_type`, `aggregate_type`, `aggregate_id`, `dedupe_key`, and `payload_json` MUST NOT change after insert.

Indexes:
- unique `(dedupe_key)`
- `(event_type, created_at)`
- `(aggregate_type, aggregate_id, created_at)`

### 5.2 `integration_event_links`

Purpose: normalized links from one event to one or more related domain rows without foreign-keying `integration_events` directly to ticket tables.

Columns:
- `id uuid primary key`
- `event_id uuid not null references integration_events(id)`
- `entity_type text not null`
  allowed values in Phase 1:
  - `ticket`
  - `ticket_message`
  - `ticket_status_history`
- `entity_id uuid not null`
- `relation_kind text not null`
  allowed values in Phase 1:
  - `primary`
  - `message`
  - `status_history`
- `created_at timestamptz not null default now()`

Required links:
- `ticket.created` MUST create:
  - one `primary` link to the ticket row
  - one `message` link to the initial public `ticket_messages` row created with the ticket
- `ticket.public_message_added` MUST create:
  - one `primary` link to the ticket row
  - one `message` link to the new public `ticket_messages` row
- `ticket.status_changed` MUST create:
  - one `primary` link to the ticket row
  - one `status_history` link to the `ticket_status_history` row that represents the change

Indexes and constraints:
- unique `(event_id, entity_type, entity_id, relation_kind)`
- `(event_id)`
- `(entity_type, entity_id)`

### 5.3 `integration_event_targets`

Purpose: per-target delivery state for one emitted event.

Columns:
- `id uuid primary key`
- `event_id uuid not null references integration_events(id)`
- `target_name text not null`
- `target_kind text not null`
  allowed value in Phase 1:
  - `slack_webhook`
- `delivery_status text not null`
  allowed values:
  - `pending`
  - `processing`
  - `sent`
  - `failed`
  - `dead_letter`
- `attempt_count integer not null default 0`
- `next_attempt_at timestamptz not null default now()`
- `locked_at timestamptz null`
- `locked_by text null`
- `last_error text null`
- `sent_at timestamptz null`
- `created_at timestamptz not null default now()`

Rules:
- One row represents one event routed to one named delivery target. The row is updated in place across retries.
- `attempt_count` MUST increment exactly once for each claimed row that reaches delivery processing, including terminal pre-send validation failures. Reclaiming stale `processing` rows without new delivery work MUST NOT increment it.
- `last_error` MUST contain a sanitized single-line operator-facing summary. It MAY include the event ID, target name, attempt count, HTTP status, and local exception class/message. It MUST NOT contain webhook URLs, secrets, request headers, request bodies, response bodies, or ticket/message text copied from `payload_json`, including internal-only content.
- `sent` and `dead_letter` are terminal states. They MUST NOT be retried automatically.

Eligibility:
- A row is eligible for claim only when:
  - `delivery_status in ('pending', 'failed')`
  - and `next_attempt_at <= now()`

State meanings:
- `pending`: never attempted and eligible when `next_attempt_at` is due.
- `processing`: currently claimed by a worker instance.
- `sent`: delivery succeeded.
- `failed`: the last attempt failed and the row is waiting for the next scheduled retry.
- `dead_letter`: delivery will not be retried automatically.

Indexes and constraints:
- unique `(event_id, target_name)`
- `(delivery_status, next_attempt_at)`
- `(locked_at)`

## 6. Event Emission Contract

### 6.1 General Rules

- Event emission MUST happen inside the same DB transaction as the ticket mutation that produced the business fact.
- If the enclosing ticket mutation rolls back, the event row, link rows, and target rows MUST also roll back.
- A single ticket mutation transaction MAY emit more than one integration event. Delivery order across different events is best-effort and MUST NOT be treated as guaranteed.
- Duplicate emission attempts for the same business fact MUST collapse on `dedupe_key`. A duplicate `dedupe_key` MUST NOT make the ticket mutation fail.
- If a duplicate `dedupe_key` is encountered, the implementation MUST reuse the existing `integration_events` row for that business fact rather than inserting another one.
- Duplicate repair of `integration_event_targets` is allowed only when the reused event currently has zero target rows.
- If the reused event has zero target rows and current routing rules in Section 8.3 require Slack routing, the implementation MUST create exactly one new `integration_event_targets` row in the same transaction, using the current `SLACK_DEFAULT_TARGET_NAME`.
- If the reused event already has one target row, duplicate handling MUST NOT create a second target row, replace the existing target name, or migrate the row to another target, even if `SLACK_DEFAULT_TARGET_NAME` or `SLACK_TARGETS_JSON` changed after the event was first emitted.

### 6.2 Event Types and Dedupe Keys

#### `ticket.created`

Meaning:
- the ticket creation transaction committed successfully

Dedupe key:
- `ticket.created:<ticket_id>`

Emission rules:
- emit exactly once per ticket
- create the `integration_events` row even when Slack delivery is disabled
- create the required event links described in Section 5.2
- MUST NOT also emit `ticket.public_message_added` for the initial `ticket_create` message
- MUST NOT emit `ticket.status_changed` for the initial `null -> new` history row created during ticket creation

#### `ticket.public_message_added`

Meaning:
- a new public `ticket_messages` row was committed after initial ticket creation

Dedupe key:
- `ticket.public_message_added:<message_id>`

Emission rules:
- emit exactly once per new public message after ticket creation
- eligible messages are public `ticket_messages` rows other than the initial `ticket_create` row
- in current Stage 1 terms, this includes public requester replies, public ops replies, public AI auto-replies, and approved AI draft publications
- draft creation, draft rejection, internal notes, internal AI analysis, and failure notes MUST NOT emit this event

#### `ticket.status_changed`

Meaning:
- a non-initial ticket status transition was committed

Dedupe key:
- `ticket.status_changed:<status_history_id>`

Emission rules:
- emit exactly once per committed non-initial status change
- the event MUST represent an actual transition from one distinct status to another
- if the implementation ever records a no-op history row, it MUST NOT emit `ticket.status_changed` for that row
- the initial `null -> new` history row created during ticket creation MUST NOT emit this event in Phase 1
- whether an action emits `ticket.public_message_added` is independent from whether it emits `ticket.status_changed`
- any action that commits a distinct non-initial status transition MUST emit `ticket.status_changed` even when that same action creates no public message; in current Stage 1 terms, AI draft creation, AI internal route-only, and AI run failure follow this rule when they move a ticket to `waiting_on_dev_ti`
- actions that create neither a public message nor a distinct non-initial status transition MUST emit no Phase 1 Slack event; in current Stage 1 terms, internal notes and draft rejection fall in this category

## 7. Payload Contract

`integration_events.payload_json` MUST be a self-sufficient event snapshot for Slack rendering.

### 7.1 Common Fields

Every payload MUST contain:
- `ticket_id`: ticket UUID as a string
- `ticket_reference`: human-readable reference such as `T-000123`
- `ticket_title`: ticket title at event time
- `ticket_status`: ticket status after the mutating transaction
- `ticket_url`: absolute AutoSac URL for the ticket detail page, built from `APP_BASE_URL`
- `occurred_at`: RFC 3339 timestamp string for the source business fact

Common rules:
- `ticket_url` MUST deep-link an authenticated internal operator to the existing Dev/TI ticket detail route and MUST equal `<APP_BASE_URL>/ops/tickets/<ticket_reference>`.
- The path segment in `ticket_url` MUST be the payload field `ticket_reference`, matching the Stage 1 route `/ops/tickets/{reference}`. `ticket_id` MUST NOT be used in the URL path.
- `occurred_at` MUST come from the source row for the business fact:
  - ticket creation time for `ticket.created`
  - message creation time for `ticket.public_message_added`
  - status-history creation time for `ticket.status_changed`
- `ticket_status` in `ticket.status_changed` MUST equal `status_to`.

### 7.2 Event-Specific Fields

`ticket.created` adds no required fields beyond the common fields.

`ticket.public_message_added` MUST also contain:
- `message_id`: message UUID as a string
- `message_author_type`: one of `requester`, `dev_ti`, `ai`, `system`
- `message_source`: the public `ticket_messages.source` value for the message
- `message_preview`: normalized preview text for Slack rendering, as defined in Section 7.3

`message_author_type` rules:
- `message_author_type` MUST equal the associated public `ticket_messages.author_type` value for the same `message_id`.
- The payload MUST be derived from the committed public message row. It MUST NOT infer authorship from the workflow actor that triggered publication, approval, or send.
- For `message_source = 'ai_draft_published'`, `message_author_type` MUST be `ai`, matching the persisted public `ticket_messages` row for the published draft.

`ticket.status_changed` MUST also contain:
- `status_from`: previous status
- `status_to`: new status

### 7.3 `message_preview` Rules

`message_preview` MUST be derived from the public message `body_text` using these rules:
- normalize all Unicode whitespace runs to a single ASCII space
- trim leading and trailing whitespace
- measure both normalized length and `SLACK_MESSAGE_PREVIEW_MAX_CHARS` in Unicode code points
- if the normalized `body_text` length is less than or equal to `SLACK_MESSAGE_PREVIEW_MAX_CHARS`, store the entire normalized body as `message_preview`
- if the normalized `body_text` length is greater than `SLACK_MESSAGE_PREVIEW_MAX_CHARS`, store the first `SLACK_MESSAGE_PREVIEW_MAX_CHARS - 3` code points followed by `...`
- if truncation occurs, the final stored preview MUST end with `...` and MUST NOT exceed `SLACK_MESSAGE_PREVIEW_MAX_CHARS`
- store `message_preview` as plain text in the payload snapshot; Slack-specific escaping is applied only when rendering `text` as required by Section 10

`message_preview` MUST NOT include:
- internal-only content
- attachment file paths
- raw Markdown formatting when plain text is already available in `body_text`

If the normalized `body_text` is empty, `message_preview` MUST be the empty string.

## 8. Target Resolution and Configuration

### 8.1 Environment Variables

The implementation MUST add and document these environment variables:

- `SLACK_ENABLED`
  boolean global circuit breaker for Slack delivery
- `SLACK_DEFAULT_TARGET_NAME`
  target name used for Phase 1 routing
- `SLACK_TARGETS_JSON`
  JSON object mapping target name to target config
- `SLACK_NOTIFY_TICKET_CREATED`
  boolean gate for creating target rows for `ticket.created`
- `SLACK_NOTIFY_PUBLIC_MESSAGE_ADDED`
  boolean gate for creating target rows for `ticket.public_message_added`
- `SLACK_NOTIFY_STATUS_CHANGED`
  boolean gate for creating target rows for `ticket.status_changed`
- `SLACK_MESSAGE_PREVIEW_MAX_CHARS`
  integer greater than or equal to 4 for `message_preview`
- `SLACK_HTTP_TIMEOUT_SECONDS`
  integer between 1 and 30 inclusive; maximum duration of one outbound Slack webhook request before it is treated as a timeout
- `SLACK_DELIVERY_BATCH_SIZE`
  integer greater than or equal to 1; maximum number of target rows claimed in one delivery poll
- `SLACK_DELIVERY_MAX_ATTEMPTS`
  integer greater than or equal to 1; maximum attempts before dead-lettering a retryable failure
- `SLACK_DELIVERY_STALE_LOCK_SECONDS`
  integer greater than `SLACK_HTTP_TIMEOUT_SECONDS`; age after which a `processing` target row is treated as abandoned and recovered for retry

### 8.2 `SLACK_TARGETS_JSON` Shape and Validity

`SLACK_TARGETS_JSON` MUST be a JSON object with this exact structure:

```json
{
  "ops_primary": {
    "enabled": true,
    "webhook_url": "https://hooks.slack.com/services/..."
  }
}
```

Rules:
- `SLACK_TARGETS_JSON` MUST parse successfully as a JSON object. Any other JSON type is invalid.
- top-level keys are `target_name`
- `target_name` MUST match `^[a-z0-9_-]+$`
- each value MUST be a JSON object
- each value MUST contain:
  - `enabled`: boolean
  - `webhook_url`: string
- additional keys MAY be present but MUST be ignored in Phase 1
- `webhook_url` MUST be a syntactically valid absolute HTTPS URL with a non-empty host
- `webhook_url` MUST NOT be blank, whitespace-only, use `http` or any non-HTTPS scheme, contain URL userinfo, or contain a fragment
- values such as `foo` and `http://example.test/hook` are invalid and MUST be treated as configuration errors, not as per-row delivery failures
- implementations MUST NOT silently trim, normalize, or repair malformed `webhook_url` values
- `webhook_url` is secret configuration and MUST NOT be stored in database rows or logs
- any violation of this section makes Slack configuration globally invalid when Section 8.3 says validation is required

### 8.3 Routing Rules

- Phase 1 routes each eligible event to at most one target: `SLACK_DEFAULT_TARGET_NAME`.
- The schema supports multiple named targets, but multi-target fan-out is out of scope for this rollout.
- An `integration_events` row MUST always be created for the emitted business fact, even when Slack is disabled.
- Event-type flag mapping is fixed in Phase 1:
  - `ticket.created` uses `SLACK_NOTIFY_TICKET_CREATED`
  - `ticket.public_message_added` uses `SLACK_NOTIFY_PUBLIC_MESSAGE_ADDED`
  - `ticket.status_changed` uses `SLACK_NOTIFY_STATUS_CHANGED`
- `SLACK_DEFAULT_TARGET_NAME` is required only when `SLACK_ENABLED = true` and at least one `SLACK_NOTIFY_*` flag is true.
- When `SLACK_ENABLED = true`, Slack configuration is globally invalid if any of the following are true:
  - `SLACK_TARGETS_JSON` violates Section 8.2
  - at least one `SLACK_NOTIFY_*` flag is true and `SLACK_DEFAULT_TARGET_NAME` is missing, blank, does not match `^[a-z0-9_-]+$`, or does not name an entry present in `SLACK_TARGETS_JSON`
- At emission time, exactly one routing outcome MUST apply in this precedence order:
  - `suppressed_slack_disabled`: `SLACK_ENABLED = false`
  - `suppressed_invalid_config`: `SLACK_ENABLED = true` and Slack configuration is globally invalid
  - `suppressed_notify_disabled`: the event-type-specific `SLACK_NOTIFY_*` flag for the event is false
  - `suppressed_target_disabled`: the selected `SLACK_DEFAULT_TARGET_NAME` exists and its target config has `enabled = false`
  - `created`: create exactly one `integration_event_targets` row for `SLACK_DEFAULT_TARGET_NAME`
- Only the `created` routing outcome may insert an `integration_event_targets` row.
- `suppressed_invalid_config` MUST still insert the `integration_events` row and required link rows, but it MUST NOT create or mutate any `integration_event_targets` row.
- Event-type notification flags gate target creation for new events only. They MUST NOT delete or rewrite existing target rows.
- Changing `SLACK_TARGETS_JSON` or `SLACK_DEFAULT_TARGET_NAME` later MUST NOT delete, rewrite, or add a second target row to an event that already has one target row.
- If a later duplicate emission reuses an existing event with zero target rows, target-row repair MUST follow Section 6.1 and can create one row only for the current `SLACK_DEFAULT_TARGET_NAME`.
- Enabling Slack or enabling an event type later MUST NOT backfill old ticket activity. Only newly emitted events after the config change may create target rows.
- If `SLACK_ENABLED` later becomes `false`, existing `pending`, `failed`, and `processing` target rows MUST remain stored, unchanged, and unclaimed until Slack is re-enabled. While Slack is disabled globally, the runtime MUST NOT perform stale-lock recovery or any other row-state mutation.
- If `SLACK_ENABLED` remains `true`, Slack configuration is otherwise valid, and a stored row's `target_name` is missing from the current `SLACK_TARGETS_JSON` or present with `enabled = false`, the next delivery attempt for that row MUST transition it to `dead_letter` without making any outbound HTTP request, as defined in Section 9.4.
- Later restoring a previously missing or disabled target MUST NOT automatically revive rows already in `dead_letter`; terminal rows remain terminal unless an operator intervenes outside this Phase 1 contract.
- If Slack configuration is globally invalid, the implementation MUST behave as follows:
  - new emissions MUST use the `suppressed_invalid_config` outcome
  - the delivery runtime MUST behave as if Slack delivery were disabled for claim, send, and stale-lock-recovery purposes
  - existing `pending`, `failed`, and `processing` target rows MUST remain unchanged and unclaimed until configuration becomes valid again
  - no row may be dead-lettered, retried, or HTTP-sent solely because global configuration is invalid
  - core ticket behavior MUST continue to operate

## 9. Delivery Runtime Contract

### 9.1 Worker Ownership

- The delivery runtime MUST execute in the worker process.
- It MUST NOT run in the synchronous request path.
- It MUST NOT serialize behind the main AI-run polling loop.
- It MUST use independent DB sessions/transactions from AI-run work.
- When `SLACK_ENABLED = false`, the delivery runtime MUST NOT claim, send, or stale-lock-recover target rows.
- When Slack configuration is globally invalid under Section 8.3, the delivery runtime MUST NOT claim, send, or stale-lock-recover target rows.

### 9.2 Claiming Work

The worker MUST claim target rows in small batches using row-level locking with `FOR UPDATE SKIP LOCKED`.

Claiming rules:
- select only eligible rows as defined in Section 5.3
- order by `next_attempt_at` ascending, then `created_at` ascending
- limit by `SLACK_DELIVERY_BATCH_SIZE`
- on claim, set:
  - `delivery_status = 'processing'`
  - `attempt_count = attempt_count + 1`
  - `locked_at = now()`
  - `locked_by = worker instance identifier`

`locked_by` MUST be a non-secret identifier that is stable for the lifetime of the worker process instance and sufficient to correlate DB state with worker logs, such as `<hostname>:<pid>` or `<hostname>:<pid>:<thread>`.

### 9.3 Rendering and HTTP Request

- Delivery MUST render the Slack message from `payload_json` only.
- For Phase 1, the sender MUST POST a JSON body of the form `{"text": "<rendered message>"}` to the configured webhook URL.
- The outbound request MUST use UTF-8 JSON with `Content-Type: application/json`.
- Each outbound webhook request MUST use `SLACK_HTTP_TIMEOUT_SECONDS` as a hard end-to-end timeout for connection and response completion.
- The sender MUST NOT follow HTTP redirects.
- The rendered message MUST stay within the behavioral rules in Section 10.

### 9.4 Success, Retry, and Dead Letter

Delivery guarantee:
- Phase 1 provides exactly-once event storage and exactly one mutable target row per `(event_id, target_name)` inside PostgreSQL, but only at-least-once externally visible Slack posting.
- Because incoming Slack webhooks expose no idempotency key, one rare duplicate Slack post is acceptable if a worker crashes or times out after Slack may have accepted the request but before the corresponding target row is durably marked `sent`.
- The runtime MUST minimize duplicates by never sending rows already in `sent` or `dead_letter`, by persisting `sent` immediately after a 2xx response in the same delivery attempt transaction, and by retrying only according to the rules below.

Success:
- a 2xx response marks the row `sent`
- on success, set:
  - `delivery_status = 'sent'`
  - `sent_at = now()`
  - `last_error = null`
  - `locked_at = null`
  - `locked_by = null`

Retryable failure:
- transport errors, ambiguous timeouts after `SLACK_HTTP_TIMEOUT_SECONDS`, HTTP 408, HTTP 429, and HTTP 5xx MUST be treated as retryable
- on retryable failure:
  - set `delivery_status = 'failed'`
  - set sanitized `last_error`
  - clear `locked_at` and `locked_by`
  - set `next_attempt_at = now() + min(60 * 2^(attempt_count - 1), 1800) seconds`, using the post-claim `attempt_count` value
- if the current `attempt_count` has reached `SLACK_DELIVERY_MAX_ATTEMPTS`, the row MUST transition to `dead_letter` instead of `failed`, and `next_attempt_at` MUST NOT schedule another automatic retry

Terminal failure:
- malformed local payloads, unsupported event types, missing target configuration at send time, disabled target configuration at send time, HTTP 3xx, and any non-2xx HTTP response not listed as retryable above MUST be treated as terminal
- on terminal failure:
  - set `delivery_status = 'dead_letter'`
  - set sanitized `last_error`
  - clear `locked_at` and `locked_by`
- pre-send terminal failures MUST NOT make any outbound HTTP request

### 9.5 Restart and Crash Recovery

- A worker crash MUST NOT lose already-committed event rows or target rows.
- `processing` rows abandoned by a crashed worker MUST be reclaimable.
- Except while Section 8.3 or Section 9.1 globally suppresses Slack delivery, the runtime MUST perform stale-lock recovery before each normal claim cycle, or on the same poll cadence, so stale rows do not remain `processing` indefinitely while the worker stays healthy.
- While Slack delivery is globally suppressed because `SLACK_ENABLED = false` or configuration is globally invalid, stale-lock recovery MUST NOT mutate `processing` rows. Those rows MUST remain unchanged until suppression ends.
- A `processing` row MUST be treated as stale when `locked_at` is older than `SLACK_DELIVERY_STALE_LOCK_SECONDS` seconds.
- Stale-lock recovery MUST set:
  - `delivery_status = 'failed'`
  - `locked_at = null`
  - `locked_by = null`
  - `last_error` to a sanitized stale-lock recovery summary
  - `next_attempt_at = now()`
- Stale-lock recovery MUST preserve the current `attempt_count`. The recovered row becomes eligible for a fresh claim, and only that later fresh claim may increment `attempt_count` again.
- Stale-lock recovery itself MUST NOT increment `attempt_count`.
- When global suppression ends, the runtime MUST run stale-lock recovery before any new claim cycle that could otherwise skip those stale `processing` rows.

## 10. Slack Message Contract

Slack is a notification surface, not a mirrored conversation transcript.

Each delivered Slack message MUST include:
- `ticket_reference`
- a concise event summary
- an absolute link back to AutoSac via `ticket_url`

Event-specific rendering requirements:
- `ticket.created` MUST include `ticket_title`
- `ticket.public_message_added` MUST include `message_author_type` and `message_preview` when the preview is non-empty
- `ticket.status_changed` MUST include both `status_from` and `status_to`

Slack text sanitization rules:
- Every user-derived field rendered into Slack `text` MUST be converted to single-line plain text before concatenation by replacing line breaks and tabs with spaces and collapsing repeated spaces.
- Every user-derived field rendered into Slack `text` MUST then escape `&` as `&amp;`, `<` as `&lt;`, and `>` as `&gt;`.
- At minimum, `ticket_title` and `message_preview` MUST follow these escaping rules.
- The renderer MUST NOT emit raw Slack control syntax from user-derived content, including raw forms such as `<!channel>`, `<!here>`, `<!everyone>`, `<@U123>`, or angle-bracket link markup.
- The only message-body content permitted in a delivered Slack message is `message_preview` as defined in Section 7.3. If the public message fits within `SLACK_MESSAGE_PREVIEW_MAX_CHARS`, that preview MAY equal the full normalized public body. The renderer MUST NOT append raw `body_text` or any expansion longer than `message_preview`.

Delivered Slack messages MUST NOT include:
- internal notes
- internal AI analysis
- attachment storage paths
- message body content beyond `message_preview`
- reviewer-only metadata
- secrets or webhook URLs

Illustrative examples:

```text
Novo ticket T-000123: Falha ao abrir o sistema
Abrir no AutoSac: https://autosac.example.local/ops/tickets/T-000123
```

```text
Nova mensagem publica em T-000123 por requester: Ainda ocorre erro ao salvar
Abrir no AutoSac: https://autosac.example.local/ops/tickets/T-000123
```

```text
T-000123 mudou de waiting_on_user para waiting_on_dev_ti
Abrir no AutoSac: https://autosac.example.local/ops/tickets/T-000123
```

The exact wording MAY vary, but the required information content above MUST be preserved.

## 11. Observability and Security

- Webhook URLs are credentials. They MUST live only in environment configuration and process memory.
- The database MUST store event facts, delivery state, timestamps, counts, and sanitized errors, but MUST NOT store webhook URLs.
- The process that performs the ticket-mutation transaction MUST emit a structured log for event emission. The worker delivery runtime MUST emit structured logs for:
  - target claim
  - send success
  - retry scheduling
  - dead-letter transition
  - runtime suppression due to invalid Slack configuration
- Emission-path logs MUST include at least `event_id`, `event_type`, `aggregate_type`, `aggregate_id`, `dedupe_key`, and `routing_result`, where `routing_result` is one of the Section 8.3 routing outcomes.
- Emission-path logs with `routing_result = 'created'` or `routing_result = 'suppressed_target_disabled'` MUST include `target_name`.
- Emission-path logs with `routing_result = 'suppressed_invalid_config'` MUST include a stable machine-readable `config_error_code` plus a sanitized `config_error_summary`. Those fields MUST distinguish, at minimum, JSON parse/type failure, invalid or missing default target selection, and invalid target entry or webhook URL.
- If no `integration_event_targets` row exists for that emission attempt, target-row state fields such as `delivery_status`, `attempt_count`, and `locked_by` MUST be omitted rather than fabricated.
- Delivery-runtime row-state logs for target claim, send success, retry scheduling, and dead-letter transition MUST include at least `event_id`, `target_name`, `delivery_status`, `attempt_count`, and `locked_by`, plus HTTP status or failure class when applicable.
- Delivery-runtime invalid-config suppression logs MUST include at least `config_error_code` and a sanitized `config_error_summary`, and MUST indicate that row claiming and stale-lock recovery were skipped for the poll cycle or equivalent runtime check. Because no row was claimed, these logs MUST omit row-state fields such as `event_id`, `target_name`, `delivery_status`, `attempt_count`, and `locked_by`.
- Slack delivery failures MUST NOT mutate ticket domain tables beyond the already-committed integration rows.
- No new UI is required in Phase 1. Operational visibility through SQL queries and worker logs is sufficient.

## 12. Minimum Test Coverage

The implementation MUST include tests covering at least:

- migration creates `integration_events`, `integration_event_links`, and `integration_event_targets` with the required constraints and indexes
- ticket creation emits exactly one `ticket.created`
- ticket creation does not emit `ticket.public_message_added` for the initial message
- ticket creation does not emit `ticket.status_changed` for the initial `null -> new` history row
- requester replies, public ops replies, public AI replies, and approved AI draft publication emit exactly one `ticket.public_message_added`
- `ticket.public_message_added.message_author_type` matches the associated public `ticket_messages.author_type`, including `ai_draft_published -> ai`
- every payload builds `ticket_url` as `<APP_BASE_URL>/ops/tickets/<ticket_reference>`
- internal notes and draft rejection emit no Phase 1 Slack event targets in current Stage 1 behavior
- AI failure notes and draft creation emit no `ticket.public_message_added` target rows
- AI failure, AI internal route-only, and draft-creation flows emit `ticket.status_changed` when they commit a distinct non-initial status change
- non-initial actual status changes emit exactly one `ticket.status_changed`
- duplicate emission attempts collapse on `dedupe_key` without failing the ticket mutation
- duplicate repair creates at most one target row: events that already have a target row never gain a second row after routing changes, while zero-target events may gain one row for the current `SLACK_DEFAULT_TARGET_NAME` only when Section 8.3 routing rules require it
- each claimed delivery attempt increments `attempt_count` exactly once, including successful sends and pre-send terminal validation failures
- `message_preview` stores the full normalized public body when it fits within `SLACK_MESSAGE_PREVIEW_MAX_CHARS`, and otherwise stores the first `SLACK_MESSAGE_PREVIEW_MAX_CHARS - 3` normalized code points plus `...`
- rendered Slack text escapes user-derived fields so requester content cannot trigger Slack mentions or angle-bracket markup
- claiming logic skips locked rows safely
- 2xx delivery marks the row `sent`
- retryable failures, including HTTP 408, HTTP 429, HTTP 5xx, and local request timeouts, leave the already-claimed attempt counted once and reschedule the row using the Section 9.4 backoff formula
- target rows whose named target is missing or disabled at send time move to `dead_letter` without an outbound HTTP request
- HTTP 3xx and non-retryable 4xx responses move the row to `dead_letter`
- outbound HTTP timeouts after `SLACK_HTTP_TIMEOUT_SECONDS` are treated as retryable failures
- stale `processing` rows are recoverable after worker restart by clearing the lock, preserving `attempt_count`, setting `delivery_status = failed`, and making the row immediately eligible again
- while `SLACK_ENABLED = false`, new emissions still record `integration_events` but create no new `integration_event_targets`, and existing `pending`/`failed`/`processing` rows remain unchanged because claim, send, and stale-lock recovery are suppressed
- malformed or non-HTTPS `webhook_url`, malformed `SLACK_TARGETS_JSON`, and missing, blank, invalid, or unmapped `SLACK_DEFAULT_TARGET_NAME` when `SLACK_ENABLED = true` and any `SLACK_NOTIFY_*` flag is true are global-invalid-config cases: new emissions still record `integration_events` but create no target rows, and existing `pending`/`failed`/`processing` rows remain unchanged until configuration becomes valid again because claim, send, and stale-lock recovery are suppressed
- once global suppression ends, stale `processing` rows are recovered under Section 9.5 before new claims proceed
- emission-path logs distinguish `suppressed_invalid_config` from `suppressed_slack_disabled`, `suppressed_notify_disabled`, and `suppressed_target_disabled`
- delivery-runtime invalid-config suppression logs omit row-state fields because no row was claimed and stale-lock recovery was also skipped

## 13. Rollout Sequence

Recommended rollout:

1. Add the migration and model changes with `SLACK_ENABLED = false`.
2. Verify that eligible ticket mutations create the expected `integration_events` rows and links without affecting existing ticket behavior.
3. Enable one named Slack target in a non-production environment.
4. Enable the desired `SLACK_NOTIFY_*` event types and verify success, retry, and dead-letter behavior.
5. Promote to production with one target only.
6. Observe duplicate rate, dead letters, and notification noise before changing which event types are enabled.

No historical backfill is part of this rollout.
