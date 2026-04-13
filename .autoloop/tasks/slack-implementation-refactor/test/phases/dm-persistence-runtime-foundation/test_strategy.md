# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: dm-persistence-runtime-foundation
- Phase Directory Key: dm-persistence-runtime-foundation
- Phase Title: DM Persistence and Runtime Foundation
- Scope: phase-local producer artifact

## Behavior-to-Coverage Map

- `AC-1` schema foundation:
  - `tests/test_slack_dm_foundation.py::test_slack_dm_foundation_migration_adds_settings_user_mapping_and_recipient_fields`
  - Confirms the phase migration adds `slack_dm_settings`, `users.slack_user_id`, DM recipient columns, and narrows `target_kind` to `slack_dm`.

- `AC-2` DB-backed runtime loading and defaults:
  - `tests/test_slack_dm_foundation.py::test_load_slack_dm_settings_defaults_to_disabled_dm_mode_when_row_missing`
  - `tests/test_slack_dm_foundation.py::test_load_slack_dm_settings_flags_undecryptable_token_and_runtime_context_uses_db_values`
  - `tests/test_slack_dm_foundation.py::test_valid_db_loaded_slack_runtime_uses_dm_routing_without_webhook_targets`
  - `tests/test_slack_event_emission.py`
  - `tests/test_slack_delivery.py`
  - `tests/test_ai_worker.py`
  - Covers missing-row defaults, DB-loaded override of injected settings, DM-mode `suppressed_no_recipients` behavior, and adjacent worker/runtime regression surfaces.

- `AC-3` token crypto and persisted health helpers:
  - `tests/test_slack_dm_foundation.py::test_encrypt_and_decrypt_slack_bot_token_round_trip`
  - `tests/test_slack_dm_foundation.py::test_upsert_slack_dm_settings_encrypts_token_and_requires_stored_token_when_enabled`
  - `tests/test_slack_dm_foundation.py::test_upsert_slack_dm_settings_preserves_existing_token_when_blank_input_is_submitted`
  - `tests/test_slack_dm_foundation.py::test_clear_slack_dm_token_disables_delivery_and_preserves_workspace_metadata`
  - `tests/test_slack_dm_foundation.py::test_persist_slack_delivery_health_upserts_system_state`
  - Covers encrypted-at-rest token storage, enable-without-token failure, blank-token retention, explicit clear-token behavior, and `system_state` delivery-health persistence.

## Preserved Invariants Checked

- DB-loaded Slack config stays authoritative for production-style runtime construction; direct `Settings.slack` injection remains a non-DB test surface only.
- Valid DB-backed DM settings no longer depend on webhook target lookup.
- Clear-token helper disables delivery without erasing non-secret workspace metadata.

## Edge Cases and Failure Paths

- Missing `slack_dm_settings` row falls back to disabled DM defaults.
- Blank token input preserves the stored ciphertext instead of clearing it implicitly.
- Enabling delivery without any stored token raises a deterministic validation error.
- Undecryptable ciphertext marks the runtime invalid without crashing load.

## Stabilization Notes

- Foundation helper tests use `_FakeStateDb` in-memory state to avoid live DB, network, and timing flake.
- Adjacent emission and worker regressions rely on existing deterministic fake-session suites instead of broad end-to-end flows.

## Known Gaps

- Admin HTML, route-layer save/disconnect behavior, and `/ops/users` Slack ID editing are deferred to later phases.
- Recipient-row insertion and Slack Web API DM delivery remain out of scope here; the current zero-target DM-mode placeholder is covered only at the foundation/runtime layer.
- Broader web-route suites still depend on optional packages outside this phase’s focused helper coverage.
