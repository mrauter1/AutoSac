# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: regression-rollout-verification
- Phase Directory Key: regression-rollout-verification
- Phase Title: Regression Completion and Rollout Verification
- Scope: phase-local producer artifact

## Behavior-to-Test Coverage Map

- AC-1 / explicit runtime boundary and first-class routing snapshot persistence
  - `tests/test_slack_event_emission.py::test_record_ticket_created_event_requires_explicit_runtime_context_even_with_session_settings`
  - `tests/test_slack_event_emission.py::test_create_requester_ticket_emits_ticket_created_only`
  - `tests/test_slack_event_emission.py::test_ticket_created_suppression_paths_record_event_and_links_without_target_row`
  - `tests/test_foundation_persistence.py::test_slack_routing_runtime_refactor_migration_adds_routing_snapshot_and_claim_token_columns`
- AC-1 / duplicate reuse edge cases
  - `tests/test_slack_event_emission.py::test_duplicate_reuse_preserves_zero_target_state_and_log_after_routing_change`
  - `tests/test_slack_event_emission.py::test_duplicate_reuse_preserves_existing_target_row_state_without_creating_second_row`
  - `tests/test_slack_event_emission.py::test_duplicate_reuse_zero_target_preserves_stored_non_created_routing_snapshot`
  - `tests/test_slack_event_emission.py::test_duplicate_reuse_zero_target_falls_back_to_suppressed_notify_disabled_for_stale_or_missing_snapshot`
- AC-1 / claim-token ownership, retry exhaustion before finalization, and canonical write sets
  - `tests/test_slack_delivery.py::test_claim_delivery_targets_marks_rows_processing_and_uses_skip_locked`
  - `tests/test_slack_delivery.py::test_recover_stale_delivery_targets_preserves_attempt_count_and_clears_lock`
  - `tests/test_slack_delivery.py::test_load_claimed_processing_target_uses_claim_token_and_for_update`
  - `tests/test_slack_delivery.py::test_classify_delivery_attempt_builds_retryable_outcome_before_finalization`
  - `tests/test_slack_delivery.py::test_classify_delivery_attempt_converts_retry_exhaustion_before_finalization`
  - `tests/test_slack_delivery.py::test_finalize_delivery_claim_uses_supplied_retryable_outcome_without_recomputing`
  - `tests/test_slack_delivery.py::test_finalize_delivery_claim_returns_ownership_lost_without_mutation`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_marks_sent_on_success`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_retries_retryable_failures`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted`
- AC-2 / no legacy payload metadata, session-settings lookup, or composite ownership dependency
  - Negative assertions for `_integration_routing` remain in the emission tests above.
  - The runtime-boundary test above intentionally omits any valid ambient-session fallback.
  - Claim loading/finalization tests assert `claim_token` ownership rather than `(locked_by, attempt_count)`.
- AC-3 / rollout notes and checks
  - `tests/test_hardening_validation.py::test_slack_docs_capture_phase1_rollout_posture`

## Preserved Invariants Checked

- `payload_json` remains free of private routing metadata.
- Duplicate reuse never repairs or creates extra target rows.
- Stale-lock recovery preserves `attempt_count` while clearing claim state.
- Finalization applies the supplied outcome write set instead of recomputing retry exhaustion.
- Disabled or invalid Slack config still suppresses claim/send/stale-lock mutation work.

## Edge Cases And Failure Paths

- Zero-target duplicate reuse with stored suppression, stale `created`, or missing routing snapshot.
- Ownership loss after claim for sent, retryable, and dead-letter paths.
- Retry exhaustion conversion before finalization.
- Missing or disabled target config, malformed payloads, retryable transport errors, and invalid-config runtime suppression.

## Stabilization Notes

- The covered Slack tests are deterministic: they use fake sessions, in-memory row objects, monkeypatched helpers, and no live network or timing-sensitive polling.
- The rollout-doc assertion stays centralized in `tests/test_hardening_validation.py` to avoid duplicated wording checks across multiple test files.

## Known Gaps

- None within the active phase scope. This pass was validation-only because the checked-in Slack regression suite already covered the requested refactor behavior.
