# Implementation Notes

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: implement
- Phase ID: schema-and-config-foundation
- Phase Directory Key: schema-and-config-foundation
- Phase Title: Schema and Config Foundation
- Scope: phase-local producer artifact

## Files changed
- `.env.example`
- `README.md`
- `docs_deployment.md`
- `shared/config.py`
- `shared/models.py`
- `shared/migrations/versions/20260410_0010_slack_integration_foundation.py`
- `tests/test_foundation_persistence.py`
- `tests/test_hardening_validation.py`

## Symbols touched
- `shared.config.SlackTargetSettings`
- `shared.config.SlackSettings`
- `shared.config._env_soft_bool`
- `shared.config._env_soft_int`
- `shared.config._is_valid_slack_webhook_url`
- `shared.config._load_slack_settings`
- `shared.config.Settings`
- `shared.config.get_settings`
- `shared.models.IntegrationEvent`
- `shared.models.IntegrationEventLink`
- `shared.models.IntegrationEventTarget`
- `shared.models.INTEGRATION_*` enum tuples

## Checklist mapping
- Milestone 1 / AC-1: added the `20260410_0010` Alembic migration plus ORM models for `integration_events`, `integration_event_links`, and `integration_event_targets`.
- Milestone 1 / AC-2: added structured Slack config parsing under `settings.slack` with soft invalid-config reporting instead of startup-fatal `SettingsError`.
- Milestone 1 / AC-3: documented the new Slack env vars and the `SLACK_ENABLED=false` rollout posture in `.env.example`, `README.md`, and `docs_deployment.md`.

## Assumptions
- The injected phase session file path was absent in the workspace; implementation proceeded from the available request, plan, criteria, feedback, and decisions artifacts.
- Slack env parsing treats malformed Slack-only values as soft-invalid runtime state even when Slack is currently disabled; later routing code can still prioritize `SLACK_ENABLED=false` before invalid-config suppression.

## Preserved invariants
- Existing non-Slack settings validation remains startup-fatal where it already was.
- Slack-specific misconfiguration does not raise during `get_settings()` or `Settings.validate_contracts()`.
- No ticket, message, status-history, or AI-run behavior was changed in this phase.

## Intended behavior changes
- `settings.slack` now exposes parsed targets, notify flags, scalar tunables, and the first stable invalid-config code/summary for Slack-specific env issues.
- When `SLACK_ENABLED=true`, missing or empty `SLACK_TARGETS_JSON` now surfaces as invalid-config state instead of being treated like an empty-but-valid target map.
- The schema now has dedicated integration tables and DB-level delivery-state checks for Phase 1 Slack persistence.

## Known non-changes
- No event emission hooks were added.
- No worker delivery logic or Slack HTTP behavior was added.
- No UI behavior changed.

## Expected side effects
- `alembic upgrade head` will now create the three integration tables.
- Operators will see Slack env knobs in docs before the delivery runtime is implemented.

## Validation performed
- `pytest tests/test_foundation_persistence.py -k slack_integration_foundation_migration_adds_required_tables_and_indexes`
- `pytest tests/test_hardening_validation.py -k 'env_example_and_readme_capture_acceptance_contract or slack_docs_capture_phase1_rollout_posture or get_settings_parses_valid_slack_runtime_config or get_settings_soft_reports_invalid_slack_config_without_raising or get_settings_soft_reports_missing_or_empty_slack_targets_json_when_enabled'`
- Re-ran reviewer-targeted validation with `pytest tests/test_hardening_validation.py -k 'get_settings_parses_valid_slack_runtime_config or get_settings_soft_reports_invalid_slack_config_without_raising or get_settings_soft_reports_missing_or_empty_slack_targets_json_when_enabled'`
- `python3 -m compileall shared/config.py shared/models.py shared/migrations/versions/20260410_0010_slack_integration_foundation.py`
- Attempted broader validation with `pytest tests/test_foundation_persistence.py tests/test_hardening_validation.py`; unrelated existing failures remain because `python-multipart` is not installed in this runner and FastAPI import-time form handling aborts the web-stack tests.

## Deduplication / centralization decisions
- Centralized Slack env parsing in `shared/config.py` under one `SlackSettings` object rather than scattering raw env access across future callers.
- Reused shared model enum tuples and migration-side checks so later emission and delivery phases can share one source of truth for allowed integration values.
