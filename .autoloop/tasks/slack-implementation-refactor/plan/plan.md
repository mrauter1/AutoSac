# Slack DM Notification Plan

## Scope Considered

- Authoritative source: the run request snapshot and DM PRD; the raw phase log contains no later clarifications.
- Baseline code inspected: `shared/config.py`, `shared/models.py`, `shared/user_admin.py`, `shared/integrations.py`, `shared/ticketing.py`, `app/routes_ops.py`, `app/routes_requester.py`, `worker/slack_delivery.py`, `worker/main.py`, Slack-focused tests, and rollout docs.
- Out of scope unless the PRD explicitly requires it: inbound Slack features, OAuth/sign-in, Slack user autodiscovery, historical backfill, mixed webhook/DM compatibility, or unrelated worker redesign.

## Baseline Delta Summary

- Runtime is still webhook-target and env-driven: `shared/config.py` parses `SLACK_*`, `shared/integrations.py` inserts one `slack_webhook` target, and `worker/slack_delivery.py` posts webhook URLs.
- No persisted Slack config or integration admin page exists. `/ops/users` has no `slack_user_id` field and currently exposes only the existing role/password management surface.
- Worker delivery builds a Slack runtime once at thread startup, which is incompatible with DB-backed config changes, token rotation, and the required per-cycle `auth.test` health check.
- Tests, `.env.example`, README, and deployment docs are anchored to the old webhook/env contract and must be updated in the same implementation slice so the repo does not carry two competing Slack contracts.

## Locked Implementation Shape

### Persistence and runtime ownership

- Add one new migration after `20260410_0011` and update `shared/models.py` for:
  - `slack_dm_settings`
  - `users.slack_user_id`
  - `integration_event_targets.recipient_user_id`
  - `integration_event_targets.recipient_reason`
  - `target_kind = 'slack_dm'` and `recipient_reason in ('requester', 'assignee', 'requester_assignee')`
- Treat existing Slack integration rows as disposable pre-launch state. Clear `integration_event_targets`, `integration_event_links`, and `integration_events` during the DM migration before enforcing DM-only recipient columns and the narrowed `target_kind` contract.
- Keep the existing routing snapshot columns on `integration_events`. For DM routing, `routing_target_name` remains null and `routing_result` changes to the DM value set: `created`, `suppressed_slack_disabled`, `suppressed_invalid_config`, `suppressed_notify_disabled`, and `suppressed_no_recipients`.
- Retire webhook-era `Settings.slack` parsing from `shared/config.py`. Environment variables remain authoritative only for application primitives such as `APP_BASE_URL`, `DATABASE_URL`, `APP_SECRET_KEY`, and generic worker settings.
- Introduce one small shared Slack DM helper/service module to own:
  - DB-backed singleton load/save with disabled defaults when the row is absent
  - form/runtime validation
  - HKDF-SHA256 plus Fernet token encryption/decryption
  - Slack Web API calls for `auth.test`, `conversations.open`, and `chat.postMessage`
  - persisted delivery-health snapshots in `system_state` so the admin page can show last-known health without live Slack calls on page load
- Add the `cryptography` dependency needed for HKDF and Fernet to `requirements.txt` as part of the same persistence/runtime slice.
- `SlackRuntimeContext` should become a per-transaction or per-cycle bundle of app settings, loaded Slack DM config, clock, and logger. Request-path emitters build it from the current DB session; the worker reloads it each delivery cycle instead of reusing startup-time config.

### Admin UI and permissions

- Add `require_admin_user` in the auth layer and use it for `/ops/integrations/slack`.
- Add `GET /ops/integrations/slack`, `POST /ops/integrations/slack`, and an explicit disconnect or clear-token POST in `app/routes_ops.py`, plus a dedicated template, nav entry, and i18n keys.
- Save flow must:
  - validate booleans and numeric knobs against the PRD ranges
  - preserve the stored token when the submitted token field is blank
  - reject enablement when no decryptable token exists
  - run `auth.test` before storing a new token
  - persist `team_id`, `team_name`, `bot_user_id`, `validated_at`, `updated_at`, and `updated_by_user_id`
  - never echo the raw token into HTML, logs, or validation errors
- Extend `/ops/users` create/update forms and `shared/user_admin.py` with optional `slack_user_id`, but only admins may set or clear it. Dev/TI-visible forms should hide the field, and non-admin POST attempts to change it should fail at the route layer instead of being silently ignored.

### Emission-time routing and persistence

- Keep `shared/integrations.py` as the owner of payload construction, dedupe reuse, link-row inserts, and emission logging.
- Reuse the webhook PRD payload rules unchanged, including message-preview sanitization and internal-content exclusion. `message_preview_max_chars` now comes from the loaded DB-backed Slack DM settings snapshot, defaulting to 200 when the singleton row is absent.
- Replace target-based routing with recipient-based routing driven by the post-mutation ticket state:
  - requester: `tickets.created_by_user_id`
  - assignee: `tickets.assigned_to_user_id`
  - eligible recipients must exist, be active, and have a nonblank `users.slack_user_id`
  - same requester and assignee collapses to one row with `recipient_reason = requester_assignee`
  - target rows use `target_name = user:<recipient_user_id>`
- New event inserts must always persist the event row and required link rows for eligible business facts, with zero, one, or two DM target rows. Duplicate reuse remains read-only and must never add recipient rows after later enablement, assignment changes, or Slack ID edits.
- Emission logs should switch from webhook `target_name` observability to `recipient_target_count`, while still logging `event_id`, `event_type`, `aggregate_id`, `dedupe_key`, and `routing_result`.

### Worker delivery and health checks

- `worker/main.py` and `worker/slack_delivery.py` should stop constructing a permanent Slack runtime at thread startup. Each delivery cycle must load the current DB-backed Slack DM config and token state.
- Before stale-lock recovery or claim work, the cycle must run `auth.test` with the current token. Auth or scope failures suppress the whole cycle, update the persisted delivery-health snapshot, and leave existing `pending`, `failed`, and `processing` rows untouched.
- Keep the existing claim-token ownership model. Change send-time execution to:
  - load the current recipient `User` by `recipient_user_id`
  - dead-letter the row without Slack HTTP calls if the user is missing, inactive, or currently lacks `slack_user_id`
  - call `conversations.open`
  - call `chat.postMessage` with the rendered Slack text body
  - require HTTP success and JSON `ok = true` on both API responses
- Classify failures as:
  - global invalid-config: missing or undecryptable token, auth failures, missing scopes
  - terminal recipient: missing or inactive recipient, missing Slack ID, recipient-specific Slack API errors
  - retryable: transport errors, ambiguous timeouts, 408, 429, 5xx, Slack `ratelimited`, or transient internal errors
- Honor `Retry-After` when present by flooring `next_attempt_at` to both the header and the normal backoff minimum. Keep logs and persisted error summaries free of tokens, ciphertext, Slack response bodies, and internal-note text.

## Milestones

### Milestone 1: Persistence and DB-backed runtime primitives

- Add the DM migration, models, the `cryptography` dependency, and the shared Slack DM settings/client helper.
- Retire env-driven Slack runtime loading and rebase the runtime context on DB-backed settings plus app env primitives.
- Wire last-known delivery-health persistence through `system_state`.

Exit criteria:

- The repo has no authoritative `SLACK_*` runtime path.
- The migration can land on a pre-launch database without requiring webhook-row compatibility.
- Token encryption/decryption, singleton defaults, and health-state storage are available to routes and worker code.

### Milestone 2: Admin surfaces and permissions

- Ship the admin-only integration page, save/disconnect routes, template, nav, and i18n.
- Extend `/ops/users` and `shared/user_admin.py` with `slack_user_id` validation, uniqueness, trimming, clear behavior, and admin-only editing.
- Update ops UI tests and locale-switch behavior for the new admin forms.

Exit criteria:

- Admins can manage Slack DM settings end-to-end from `/ops/integrations/slack`.
- Admins can create, update, clear, and validate `slack_user_id` mappings from `/ops/users`.
- Non-admin users cannot change Slack configuration or Slack user IDs.

### Milestone 3: DM emission-time routing

- Replace webhook target selection in `shared/integrations.py` with recipient routing and DM target-row inserts.
- Preserve canonical payloads, dedupe keys, and no-backfill duplicate behavior.
- Update emission logging and tests for `suppressed_no_recipients`, recipient collapse, and current-ticket-state routing.

Exit criteria:

- New eligible events create 0, 1, or 2 `slack_dm` target rows exactly as the PRD specifies.
- Duplicate reuse never repairs or backfills missing recipient rows.
- Emission-time routing uses DB-backed config and current active Slack IDs without leaking internal content.

### Milestone 4: Worker DM delivery, docs, and regression completion

- Replace webhook sending with Slack Web API DM delivery and auth preflight.
- Update worker orchestration so each cycle reloads DB-backed config and health.
- Rewrite Slack docs and tests to remove webhook/env expectations and verify the DM rollout contract.

Exit criteria:

- The worker only sends Slack DMs through `conversations.open` plus `chat.postMessage`.
- Retry, dead-letter, invalid-config suppression, and send-time recipient lookup match the PRD.
- README, `.env.example`, deployment docs, and tests describe one DB-backed Slack DM contract rather than a competing webhook contract.

## Compatibility, Migration, Rollout, and Rollback

- This rollout intentionally replaces the unreleased webhook-target design. No mixed webhook/DM runtime and no compatibility bridge for old target rows are planned.
- The DM migration should explicitly clear disposable pre-launch Slack integration rows before tightening `integration_event_targets` to DM-only recipient semantics.
- `APP_SECRET_KEY` remains the only env input into Slack secret handling. If it changes, the stored token becomes undecryptable and Slack must surface as invalid until an admin saves a new token.
- Deploy web and worker together after the migration. Keep Slack disabled in the DB until the UI, health snapshot, and new target rows verify correctly.
- Rollback is config-first: disable Slack in `slack_dm_settings` or disconnect the token. A code rollback may leave additive schema in place, but it should not restore env-driven webhook compatibility.

## Regression Controls

- Preserve the existing event families, payload fields, internal-note exclusions, and no-backfill rules from the webhook PRD.
- Keep all Slack HTTP work out of the synchronous request path; ticket mutations must still succeed while Slack is disabled or broken.
- Guard admin-only surfaces at both template and route layers so Dev/TI permissions do not expand accidentally through hidden form fields.
- Centralize Slack Web API response parsing and sanitization so success detection and secret redaction do not drift across `auth.test`, `conversations.open`, and `chat.postMessage`.
- Make delivery-health persistence advisory only; it must never become the source of truth for routing, claiming, or send-time recipient resolution.

## Test and Verification Plan

- Persistence/config tests:
  - new migration adds `slack_dm_settings`, `users.slack_user_id`, and DM recipient columns/constraints
  - token encryption is at rest only, blank token preserves the stored token, clear-token disables delivery and removes ciphertext
  - undecryptable tokens surface invalid config without crashing startup
- Admin/UI tests:
  - admin-only access to `/ops/integrations/slack`
  - save/disconnect page behavior, locale-switch path stability, and no token echo
  - `/ops/users` admin-only Slack ID edits, uniqueness, trim/clear handling, and Dev/TI rejection paths
- Emission tests:
  - requester-only, assignee-only, requester-plus-assignee, and requester-equals-assignee collapse
  - zero-recipient suppression and `recipient_target_count` logging
  - duplicate reuse after later Slack ID or assignment changes creates no new rows
- Delivery tests:
  - cycle-level `auth.test` suppression and health persistence
  - send-time missing or inactive recipient dead-letters without Slack calls
  - `conversations.open` then `chat.postMessage`, with `ok = true` required on both
  - auth/scope, recipient, transport, timeout, 429 or `Retry-After`, and 5xx classification
- Docs/contract tests:
  - remove webhook/env assumptions from README, `.env.example`, deployment docs, and hardening tests
  - keep the DM PRD and code/tests aligned on the DB-backed source of truth

## Risk Register

- R1: Leaving env-backed Slack parsing in place creates two competing runtime contracts.
  Mitigation: remove `Settings.slack` as an authoritative interface and update docs/tests in the same change set.
- R2: Tightening `integration_event_targets` to DM-only rows breaks migration on databases with dry-run webhook rows.
  Mitigation: clear Slack integration tables as allowed pre-launch data before enforcing DM recipient constraints.
- R3: Worker threads keep stale Slack config or token state after admin changes.
  Mitigation: reload DB-backed Slack runtime each cycle and cover token rotate/disable flows in worker tests.
- R4: Dev/TI users gain unintended control over `slack_user_id` through reused `/ops/users` forms.
  Mitigation: hide the field unless the actor is an admin and reject non-admin submissions server-side.
- R5: Delivery success/failure classification regresses because Slack Web API requires both HTTP success and JSON `ok`.
  Mitigation: centralize Web API response parsing and add explicit tests for HTTP-success and `ok=false` cases on both API calls.
- R6: APP secret rotation or decryption failures become silent delivery drops.
  Mitigation: treat undecryptable tokens as explicit invalid config, persist health or error state, and surface it in the admin UI.
