# PRD — Slack DM Notifications for AutoSac

Document status: Draft
Audience: implementation agent, reviewer, system owner

Normative terms:
- MUST = mandatory
- MUST NOT = prohibited
- SHOULD = recommended
- MAY = optional

## 1. Purpose

This document defines a new Slack direct-message notification contract for AutoSac.

The goal is to notify the ticket requester and the currently assigned internal member through Slack DMs, without making Slack part of the source-of-truth workflow. PostgreSQL remains authoritative. Slack remains an asynchronous projection of selected ticket events.

## 1.1 Relationship to Existing Slack Docs

This document is separate from the existing webhook-target Slack PRDs:

- [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md)
- [slack_implementation_refactor_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_implementation_refactor_PRD.md)

For the DM rollout defined here:

- the existing event families and payload snapshot rules from the webhook PRD remain canonical unless this document explicitly overrides them
- configuration source, recipient routing, target-row shape, and transport behavior are superseded by this document
- incoming webhooks to channels are not the delivery mechanism
- Slack delivery configuration is no longer sourced from `SLACK_*` environment variables

## 1.2 Pre-Launch Replacement Assumption

The webhook-based Slack feature has not been rolled out in a production-preservation sense. A conforming implementation MAY replace the unreleased webhook-target design rather than supporting both webhook and DM delivery simultaneously.

Therefore:

- no backward compatibility with pre-launch webhook-target rows is required
- no migration bridge from `slack_webhook` targets to DM targets is required
- no historical Slack backfill is required
- disposable pre-launch Slack rows from earlier dry runs MAY be cleared during rollout

## 2. Goals

The DM rollout MUST deliver these product outcomes:

1. Admins can configure Slack integration from the AutoSac UI, with configuration persisted in PostgreSQL.
2. Admins can set and update Slack user IDs for AutoSac users.
3. Eligible Slack notifications are sent as DMs to:
   - the user who opened the ticket
   - the user currently assigned to the ticket
4. Delivery remains asynchronous, worker-owned, restart-safe, and observable.
5. Enabling the feature, changing assignments, or updating Slack user IDs does not backfill old ticket activity.

## 3. Non-Goals

This feature does not:

- add inbound Slack messages, slash commands, buttons, threads, or interactive actions
- add Slack OAuth install flow, Slack Sign In, or Slack-based auth for AutoSac
- auto-discover Slack user IDs by email or directory lookup
- add per-user opt-in, mute, or subscription preferences
- add channel, group DM, or broadcast delivery
- backfill notifications for old events after enablement, assignment changes, or Slack user ID changes
- make Slack delivery part of ticket success/failure semantics
- reuse `ai_runs` as a notification queue

## 4. Preserved AutoSac Invariants

The DM integration MUST preserve these existing AutoSac invariants:

- ticket creation, replies, assignment, AI actions, and status changes MUST succeed even when Slack is disabled, misconfigured, rate-limited, or unavailable
- internal notes and any content visible only in `ticket_messages.visibility = internal` MUST NEVER be sent to Slack
- PostgreSQL remains the single system of record
- Slack delivery MUST remain asynchronous in the worker process, not in the synchronous request path
- event storage and target-row mutation MUST remain restart-safe and transactionally separate from Slack HTTP calls
- there is still no historical backfill

## 5. Architecture Summary

The DM design has four persisted layers:

1. `slack_dm_settings`
   - singleton DB-backed Slack integration configuration
   - authoritative source for enablement, token presence, notify flags, and delivery knobs

2. `users.slack_user_id`
   - optional per-user Slack identity mapping
   - admin-managed in AutoSac

3. `integration_events` and `integration_event_links`
   - append-only business facts captured in the same DB transaction as the ticket mutation

4. `integration_event_targets`
   - one mutable delivery row per unique DM recipient for one event

The delivery runtime MUST remain in the existing worker process and MUST use the current integration-event/delivery-state architecture rather than inventing a separate queue.

## 6. Admin UX and Workflow

### 6.1 Slack Integration View

The web app MUST expose an admin-only integration page at `/ops/integrations/slack`.

This page is the canonical UI for Slack DM configuration. It MUST NOT require environment-variable editing for day-to-day Slack administration.

The page MUST show:

- whether Slack DM delivery is enabled
- whether a bot token is currently stored
- the last verified Slack workspace metadata, if known
- current delivery-health status, if known
- the notify flags for each event family
- delivery/runtime tuning fields
- the last update timestamp and updating admin, if known

### 6.2 Config Fields

The page MUST allow editing at least these fields:

- `enabled`
- `bot_token` (write-only secret input)
- `notify_ticket_created`
- `notify_public_message_added`
- `notify_status_changed`
- `message_preview_max_chars`
- `http_timeout_seconds`
- `delivery_batch_size`
- `delivery_max_attempts`
- `delivery_stale_lock_seconds`

### 6.3 Save Behavior

On save:

- server-side validation MUST run before persistence
- the bot token MUST NOT be stored in plaintext
- a successful save with a new token MUST call Slack `auth.test`
- the save MUST persist returned non-secret workspace metadata such as `team_id`, `team_name`, and `bot_user_id`
- the raw token MUST NOT be rendered back into HTML after save

If the token input is blank during an edit and a token is already stored:

- the existing stored token MUST be retained

If Slack DM delivery is being enabled and no stored token exists:

- the save MUST be rejected

The page MUST also provide an explicit disconnect/clear-token action. That action MUST:

- disable Slack DM delivery
- remove the stored encrypted bot token
- preserve non-secret audit metadata unless the implementation intentionally clears it

### 6.4 User Slack ID Management

Slack user IDs MUST be managed through the existing user-management surface at `/ops/users`.

The user create/edit UI MUST gain an optional `slack_user_id` field.

Rules:

- only admins may edit `slack_user_id`
- `slack_user_id` is optional
- blank input clears the mapping
- the value MUST be trimmed before persistence
- whitespace-only values are invalid
- duplicate non-null Slack user IDs across different AutoSac users are invalid

## 7. Persisted Model

### 7.1 `slack_dm_settings`

Purpose: singleton authoritative Slack DM configuration persisted in PostgreSQL.

Columns:

- `singleton_key text primary key`
  - fixed value: `'default'`
- `enabled boolean not null default false`
- `bot_token_ciphertext text null`
- `team_id text null`
- `team_name text null`
- `bot_user_id text null`
- `validated_at timestamptz null`
- `notify_ticket_created boolean not null default false`
- `notify_public_message_added boolean not null default false`
- `notify_status_changed boolean not null default false`
- `message_preview_max_chars integer not null default 200`
- `http_timeout_seconds integer not null default 10`
- `delivery_batch_size integer not null default 10`
- `delivery_max_attempts integer not null default 5`
- `delivery_stale_lock_seconds integer not null default 120`
- `updated_by_user_id uuid null references users(id)`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Rules:

- there is exactly one logical row, keyed by `singleton_key = 'default'`
- absence of the row MUST behave the same as disabled defaults with no token stored
- `message_preview_max_chars` MUST be greater than or equal to 4
- `http_timeout_seconds` MUST be between 1 and 30 inclusive
- `delivery_batch_size` MUST be greater than or equal to 1
- `delivery_max_attempts` MUST be greater than or equal to 1
- `delivery_stale_lock_seconds` MUST be greater than `http_timeout_seconds`

### 7.2 Bot Token Storage

`bot_token_ciphertext` MUST store the Slack bot token encrypted at rest.

A conforming implementation MUST use authenticated symmetric encryption. The required baseline is:

- derive a 32-byte key from `APP_SECRET_KEY` using HKDF-SHA256
- use fixed HKDF info string `autosac-slack-dm-token-v1`
- store the encrypted token as a text-safe ciphertext blob

The implementation MAY use `cryptography` and Fernet to satisfy this requirement.

The bot token MUST NOT be stored in:

- plaintext DB columns
- logs
- `integration_events.payload_json`
- `integration_event_targets.last_error`
- rendered HTML after the save response completes

If `APP_SECRET_KEY` changes and decryption no longer works:

- Slack DM delivery MUST behave as disabled-invalid config until an admin saves a new token
- the UI MUST surface that the stored token can no longer be used

Key rotation beyond replacing the token manually is out of scope for this feature.

### 7.3 `users`

`users` MUST gain:

- `slack_user_id text null`

Rules:

- non-null values MUST be unique across `users`
- blank strings MUST NOT be stored
- this field is an operator-managed identity mapping, not a secret
- requesters MUST NOT be able to edit their own `slack_user_id` from requester-facing pages

### 7.4 `integration_event_targets`

The existing target table remains the mutable delivery-state layer, but its meaning changes from one configured webhook target per event to one unique DM recipient per event.

A conforming DM implementation MUST support at least:

- `target_kind = 'slack_dm'`

Additional DM-recipient fields:

- `recipient_user_id uuid not null references users(id)`
- `recipient_reason text not null`
  - allowed values:
    - `requester`
    - `assignee`
    - `requester_assignee`

`target_name` remains the immutable uniqueness key, but in the DM design it MUST equal:

- `user:<recipient_user_id>`

Rules:

- uniqueness remains one row per `(event_id, target_name)`
- this guarantees requester and assignee collapse to one row when they are the same AutoSac user
- later ticket assignment changes MUST NOT rewrite existing target rows for already-emitted events
- later Slack user ID changes MUST NOT rewrite existing target rows, but delivery MAY use the current `users.slack_user_id` for the stored `recipient_user_id` at send time

The existing delivery-state columns and their semantics remain canonical:

- `delivery_status`
- `attempt_count`
- `next_attempt_at`
- `locked_at`
- `locked_by`
- `claim_token`
- `last_error`
- `sent_at`
- `dead_lettered_at`

## 8. Configuration Source and Runtime Loading

Slack DM configuration is DB-backed. A conforming implementation MUST NOT treat `SLACK_ENABLED`, `SLACK_TARGETS_JSON`, `SLACK_DEFAULT_TARGET_NAME`, or `SLACK_NOTIFY_*` environment variables as the authoritative source for DM routing or DM delivery.

Environment variables remain authoritative only for application-level primitives such as:

- `APP_BASE_URL`
- `APP_SECRET_KEY`
- generic worker settings unrelated to Slack DM recipient selection

Request-path emission code and worker delivery code MUST load Slack DM configuration from PostgreSQL.

## 9. Recipient Routing Contract

### 9.1 Candidate Recipients

For each emitted Slack event, the candidate recipients are determined from the post-mutation ticket state:

1. requester: `tickets.created_by_user_id`
2. assignee: `tickets.assigned_to_user_id`, if not null

### 9.2 Recipient Eligibility

A candidate recipient is eligible for target-row creation only when:

- the user row exists
- `users.is_active = true`
- `users.slack_user_id` is non-null and non-blank

### 9.3 Recipient Collapse

If requester and assignee are the same AutoSac user:

- exactly one target row MUST be created
- `recipient_reason` MUST be `requester_assignee`

### 9.4 No Actor Suppression

This rollout is recipient-based, not actor-based.

Therefore:

- a requester MAY receive a DM for their own ticket creation
- an assignee MAY receive a DM for an action they themselves performed
- the implementation MUST NOT silently suppress self-notifications unless a future PRD changes the contract

### 9.5 No-Recipient Outcome

If global Slack delivery is otherwise enabled for the event type but zero unique eligible recipients exist for the event:

- the `integration_events` row and required link rows MUST still be created
- zero `integration_event_targets` rows MUST be created
- the observable routing outcome MUST be `suppressed_no_recipients`

Partial recipient creation is allowed:

- if one eligible unique recipient exists, create one target row
- if two different eligible recipients exist, create two target rows
- `created` means one or more target rows were created

## 10. Event and Payload Contract

The business event families and payload snapshot rules from [slack_integration_PRD.md](/home/marcelo/code/AutoSac/tasks/slack_integration_PRD.md) remain canonical for:

- `ticket.created`
- `ticket.public_message_added`
- `ticket.status_changed`
- dedupe keys
- payload fields
- payload sanitization
- public-message preview rules
- internal-content exclusion

This DM document changes only:

- where configuration lives
- how recipients are selected
- how many target rows an event may create
- how delivery is transported to Slack

## 11. No Historical Backfill

The DM rollout MUST NOT backfill old activity.

Specifically:

- enabling Slack DM delivery later MUST NOT create target rows for already-stored events
- adding a Slack user ID to a user later MUST NOT create target rows for old events
- assigning a ticket later MUST NOT create assignee-recipient rows for already-stored events
- duplicate event reuse MUST NOT repair, add, or migrate missing recipient rows for an old event

Duplicate handling MUST preserve the existing event and recipient-row state exactly as stored.

## 12. Emission-Time Routing Outcomes

At emission time, exactly one event-level routing result MUST apply:

- `suppressed_slack_disabled`
- `suppressed_invalid_config`
- `suppressed_notify_disabled`
- `suppressed_no_recipients`
- `created`

Rules:

- `created` means one or more recipient target rows were inserted
- `suppressed_invalid_config` means the DB-backed Slack DM configuration is structurally unusable for new routing, for example because the row is enabled but no decryptable token exists
- `routing_target_name` from the webhook-era design is not meaningful for DM routing and MUST be null if that column remains in use

## 13. Delivery Transport Contract

### 13.1 Slack API Model

This rollout uses Slack Web API direct-message delivery, not incoming webhooks.

A conforming send attempt MUST:

1. resolve the current recipient user row by `recipient_user_id`
2. load the current `users.slack_user_id`
3. open a DM conversation through Slack `conversations.open` for exactly one Slack user ID
4. send the rendered message through Slack `chat.postMessage`

The implementation MUST NOT rely on incoming webhook URLs, channel-based webhook posting, or Slack App Home delivery semantics.

### 13.2 Required Token Capabilities

The saved bot token MUST be suitable for:

- `auth.test`
- `conversations.open`
- `chat.postMessage`

The integration page MUST document the required Slack bot scopes. At minimum, the bot token is expected to support DM open/send behavior rather than channel-webhook behavior.

Before claiming work in a delivery cycle, the worker MUST run a lightweight Slack credential health check such as `auth.test`.

If that health check fails with an auth-level or scope-level error:

- the worker MUST treat Slack DM delivery as invalid-config for that cycle
- the worker MUST perform no claim, send, or stale-lock recovery work in that cycle

### 13.3 Success Criteria

A DM delivery attempt succeeds only when:

- `conversations.open` returns HTTP success and Slack JSON `ok = true`
- `chat.postMessage` returns HTTP success and Slack JSON `ok = true`

`chat.postMessage` MUST send the message body through the Slack `text` field. Blocks, attachments, or richer interactive payloads are out of scope for this rollout.

Parsing only the HTTP status code is insufficient for Web API success classification.

## 14. Send-Time Recipient Resolution

The worker MAY read the `users` table at send time for recipient resolution. This does not violate the event-payload immutability contract because user Slack IDs are delivery configuration, not business event content.

Send-time rules:

- if the recipient user row is missing, delivery MUST dead-letter the row with a terminal recipient error
- if the recipient user row is inactive, delivery MUST dead-letter the row with a terminal recipient error
- if `users.slack_user_id` is currently missing or blank, delivery MUST dead-letter the row with a terminal recipient error
- if the user’s Slack ID changed after the event was emitted, delivery MUST use the current mapped Slack user ID for the stored `recipient_user_id`

## 15. Failure Classification

### 15.1 Global Configuration Failures

These are global-invalid-config failures for DM delivery:

- missing or undecryptable stored bot token
- Slack auth failures such as `invalid_auth`, `not_authed`, `token_revoked`, or equivalent auth-level API errors
- missing required scopes discovered at runtime

When the request path can determine that Slack DM configuration is structurally unusable from persisted DB state, for example because the feature is enabled but no decryptable token exists:

- new emissions MUST record `suppressed_invalid_config`

When the worker discovers auth-level or scope-level invalid config from Slack at runtime:

- the worker MUST skip claim/send/stale-lock recovery work for the affected cycle and subsequent cycles until the credential health check succeeds again
- request-path emissions are not required to suppress new events solely because the worker discovered a remote auth failure that is not derivable from persisted DB state

Whenever invalid-config suppression is in effect in either case:

- existing pending, failed, and processing rows MUST remain unchanged until configuration becomes usable again

### 15.2 Terminal Recipient Failures

These are terminal row-local failures:

- missing recipient user row
- inactive recipient user
- missing `users.slack_user_id`
- Slack API recipient errors such as `user_not_found`, `channel_not_found`, `cannot_dm_bot`, `is_bot`, `user_disabled`, or equivalent recipient-specific failures

Terminal recipient failures MUST transition the row to `dead_letter`.

### 15.3 Retryable Failures

These remain retryable:

- transport errors
- ambiguous timeouts
- HTTP 408
- HTTP 429
- HTTP 5xx
- Slack API `ratelimited`
- Slack API transient internal errors

Retry scheduling MUST reuse the existing worker backoff contract unless a future PRD supersedes it.

If Slack returns HTTP 429 with a valid `Retry-After` header, the next attempt MUST be scheduled no earlier than that header value and no earlier than the normal exponential backoff floor.

## 16. Observability

Emission-path logs MUST include at least:

- `event_id`
- `event_type`
- `aggregate_id`
- `dedupe_key`
- `routing_result`
- `recipient_target_count`

Delivery-runtime logs MUST include at least:

- `event_id`
- `target_name`
- `recipient_user_id`
- `recipient_reason`
- `delivery_status`
- `attempt_count`
- `locked_by`

Logs and DB error fields MUST NOT contain:

- plaintext bot tokens
- ciphertext blobs
- Slack API response bodies that may contain secrets
- internal note text

## 17. Rollout and Migration

### 17.1 Schema Changes

A conforming implementation MUST add or change at least:

- `slack_dm_settings`
- `users.slack_user_id`
- DM-recipient fields on `integration_event_targets`
- target-kind support for `slack_dm`

### 17.2 Pre-Launch Replacement

Because the webhook rollout was never deployed as a preservation target:

- existing pre-launch Slack integration rows MAY be cleared
- existing webhook-specific target kinds MAY be removed or ignored
- existing `SLACK_*` docs and env knobs MAY remain for historical reference but are not the runtime contract for DM delivery

### 17.3 No Backfill

Rollout MUST preserve the no-backfill rule:

- the migration MUST NOT synthesize new recipient rows for old events
- enabling the feature later only affects newly emitted events

## 18. Required Tests

The implementation MUST add or update tests covering at least:

- admin-only Slack integration page load/save/disable behavior
- bot token is encrypted at rest and never rendered back after save
- blank token on edit preserves the stored token
- clearing the token disables delivery and removes the stored secret
- `users.slack_user_id` create/edit validation and uniqueness
- requester-only recipient creation
- assignee-only recipient creation
- requester-plus-assignee recipient creation
- requester-equals-assignee collapse into one target row with `recipient_reason = requester_assignee`
- no target rows when no eligible recipients have Slack IDs
- duplicate event reuse does not create missing recipient rows after later Slack ID or assignment changes
- send-time lookup uses current `users.slack_user_id`
- missing or inactive recipient dead-letters without Slack HTTP calls
- delivery uses `conversations.open` then `chat.postMessage`
- Slack Web API success requires both HTTP success and `ok = true`
- auth/token failures suppress delivery globally
- retryable transport, timeout, 429, and 5xx behavior
- no historical backfill after enablement

## 19. Acceptance Criteria

This feature is complete when all of the following are true:

1. Admins can enable, disable, and update Slack DM configuration from `/ops/integrations/slack` with configuration persisted in PostgreSQL.
2. Admins can set and update `users.slack_user_id` from `/ops/users`.
3. New eligible events create DM target rows for the requester and current assignee, collapsed by unique AutoSac user.
4. Delivery is performed by the worker through Slack Web API DM calls, not incoming webhooks.
5. Slack delivery remains asynchronous and non-blocking to ticket mutations.
6. No historical backfill occurs after enablement, reassignment, or Slack ID updates.
7. Secrets are not stored or exposed in plaintext.
