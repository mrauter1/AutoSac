# Test Strategy

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: test
- Phase ID: schema-and-config-foundation
- Phase Directory Key: schema-and-config-foundation
- Phase Title: Schema and Config Foundation
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- Migration source shape
  - Covered by `tests/test_foundation_persistence.py::test_slack_integration_foundation_migration_adds_required_tables_and_indexes`
  - Checks all three integration tables plus the required uniqueness and lookup/eligibility indexes named in the phase contract.

- Slack config happy path
  - Covered by `tests/test_hardening_validation.py::test_get_settings_parses_valid_slack_runtime_config`
  - Verifies parsed targets, notify flags, scalar tunables, and stable target lookup.

- Slack config failure paths
  - Covered by:
    - `test_get_settings_soft_reports_invalid_slack_config_without_raising`
    - `test_get_settings_soft_reports_missing_or_empty_slack_targets_json_when_enabled`
    - `test_get_settings_soft_reports_non_object_slack_targets_json_when_enabled`
    - `test_get_settings_soft_reports_missing_default_target_when_notify_enabled`
    - `test_get_settings_soft_reports_invalid_target_webhook_url`
  - Checks parse failure, missing/empty target JSON, wrong JSON top-level type, missing default target selection when notify is enabled, and invalid HTTPS webhook validation.

- Preserved invariants
  - Each Slack invalid-config test calls `settings.validate_contracts()` after `get_settings()` to confirm Slack-specific misconfiguration remains soft and non-fatal.
  - Docs/env coverage in `test_env_example_and_readme_capture_acceptance_contract` and `test_slack_docs_capture_phase1_rollout_posture` checks that the rollout stays dark by default and the new env surface is documented.

## Edge cases covered

- `SLACK_TARGETS_JSON` unset
- `SLACK_TARGETS_JSON=""`
- `SLACK_TARGETS_JSON` malformed JSON
- `SLACK_TARGETS_JSON` valid JSON but not an object
- valid target object with non-HTTPS webhook URL
- notify flag enabled without `SLACK_DEFAULT_TARGET_NAME`

## Failure paths covered

- Stable `config_error_code` / `config_error_summary` for the main invalid-config categories introduced in this phase.
- No Slack failure path test expects a startup-fatal `SettingsError`.

## Stabilization / flake control

- Tests are pure config-helper and source-file assertions; no network, timing, or external services are involved.
- Broader FastAPI smoke checks are intentionally not part of this phase’s acceptance signal because `python-multipart` is missing in this runner and fails route import before the phase-local Slack assertions run.

## Known gaps

- This phase does not yet exercise event emission or worker delivery because those behaviors are explicitly out of scope.
- Target-entry variants beyond invalid webhook shape, such as malformed `enabled` type or invalid target-name regex, remain uncovered and can be added alongside later delivery/runtime work if that surface changes again.
