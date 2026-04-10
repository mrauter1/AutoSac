# Implementation Notes

- Task ID: slack-implementation-refactor
- Pair: implement
- Phase ID: dm-persistence-runtime-foundation
- Phase Directory Key: dm-persistence-runtime-foundation
- Phase Title: DM Persistence and Runtime Foundation
- Scope: phase-local producer artifact

## Files changed

- `shared/migrations/versions/20260410_0012_slack_dm_persistence_runtime_foundation.py`
- `shared/models.py`
- `shared/slack_dm.py`
- `shared/config.py`
- `shared/integrations.py`
- `shared/ticketing.py`
- `app/routes_requester.py`
- `app/routes_ops.py`
- `worker/main.py`
- `worker/queue.py`
- `worker/slack_delivery.py`
- `worker/triage.py`
- `requirements.txt`
- `tests/test_slack_dm_foundation.py`
- `tests/test_slack_delivery.py`
- `tests/test_ai_worker.py`
- `tests/test_hardening_validation.py`

## Symbols touched

- `shared.models.User.slack_user_id`
- `shared.models.IntegrationEventTarget.recipient_user_id`
- `shared.models.IntegrationEventTarget.recipient_reason`
- `shared.models.SlackDMSettings`
- `shared.config.SlackSettings`
- `shared.config.SlackSettings.routing_mode`
- `shared.integrations.SlackRuntimeContext`
- `shared.integrations.build_slack_runtime_context`
- `shared.integrations.resolve_routing_decision`
- `shared.slack_dm.load_slack_dm_settings`
- `shared.slack_dm.upsert_slack_dm_settings`
- `shared.slack_dm.clear_slack_dm_token`
- `shared.slack_dm.encrypt_slack_bot_token`
- `shared.slack_dm.decrypt_slack_bot_token`
- `shared.slack_dm.persist_slack_delivery_health`
- `worker.slack_delivery.build_worker_slack_runtime_context`
- `worker.slack_delivery.run_delivery_cycle`
- `shared.ticketing.ensure_system_state_defaults`

## Checklist mapping

- `AC-1`: added `slack_dm_settings`, `users.slack_user_id`, DM recipient columns and constraints, and a migration that clears disposable pre-launch Slack integration rows before enforcing `target_kind = 'slack_dm'`.
- `AC-2`: removed authoritative `SLACK_*` parsing from `shared/config.py`; request routes and worker flows now build Slack runtime from the current DB session or delivery cycle, and DB-loaded valid Slack config no longer depends on webhook target fields.
- `AC-3`: added HKDF-SHA256 plus Fernet token crypto and persisted last-known Slack delivery health in `system_state`.

## Assumptions

- Existing webhook-era Slack integration rows are disposable pre-launch data per the PRD.
- Later phases own recipient-based target insertion and Slack Web API DM send transport; this phase only lands the persistence and runtime primitives they depend on.
- Direct `Settings.slack` injection remains a test-only webhook-mode surface for non-DB unit coverage, while production request and worker flows use DB-loaded DM mode.

## Preserved invariants

- Request-path ticket mutations still do not make Slack HTTP calls.
- Worker/request code now reloads Slack configuration from PostgreSQL, but delivery-health state remains advisory only and is not used as routing truth.
- Existing unit tests can still inject `Settings.slack` directly when they do not model a real DB session.

## Intended behavior changes

- `SLACK_*` environment variables no longer control authoritative Slack runtime behavior.
- The worker Slack delivery thread now receives base `Settings` and rebuilds the Slack runtime snapshot each cycle instead of keeping a startup-time snapshot.
- `system_state` now seeds `slack_dm_delivery_health` with `{"status": "unknown"}`.
- DB-loaded valid Slack DM config now records zero-target request-path events as `suppressed_no_recipients` instead of falling back to webhook target lookup or `suppressed_invalid_config`.

## Known non-changes

- No admin Slack integration HTML, routes, or forms yet.
- No recipient-based DM target-row creation yet.
- No Slack Web API DM send path in `worker/slack_delivery.py` yet.

## Expected side effects

- Slack runtime defaults to disabled unless a `slack_dm_settings` row exists.
- Migration `20260410_0012` deletes pre-launch `integration_events`, `integration_event_links`, and `integration_event_targets` rows before tightening the Slack schema surface.
- Any real database write to `integration_event_targets` now must use `target_kind = 'slack_dm'` with recipient fields populated; webhook target rows are no longer schema-valid after the migration.

## Validation performed

- `python3 -m compileall shared app worker tests`
- `python3 -m pytest tests/test_slack_dm_foundation.py tests/test_slack_delivery.py tests/test_ai_worker.py -q`
- `python3 -m pytest tests/test_slack_event_emission.py tests/test_hardening_validation.py -q`
- `python3 -m pytest tests/test_slack_dm_foundation.py tests/test_slack_event_emission.py -q`
- `python3 -m pytest tests/test_slack_delivery.py tests/test_ai_worker.py -q`
- `python3 -m pytest tests/test_hardening_validation.py -q`
- Broader web-route suites in `tests/test_auth_requester.py` and `tests/test_ops_workflow.py` are currently blocked in this environment by missing installed dependencies (`python-multipart` and `bleach`), so they were not usable as signal for this phase.

## Deduplication / centralization

- Centralized Slack DM DB load/save, token crypto, delivery-health persistence, and Slack Web API helpers in `shared/slack_dm.py`.
- Reused `SlackRuntimeContext` as the single explicit carrier for app settings plus the DB-loaded Slack runtime snapshot.
- Centralized the DB-vs-test routing split on `SlackSettings.routing_mode` so production DM runtime loading no longer depends on webhook target configuration while legacy non-DB tests can still inject explicit webhook targets.
