# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: delivery-claim-outcome-finalization
- Phase Directory Key: delivery-claim-outcome-finalization
- Phase Title: Delivery Claim, Outcome, and Finalization Refactor
- Scope: phase-local producer artifact

## Test Targets

- `tests/test_slack_delivery.py`

## Behavior-to-Coverage Map

- AC-1 / claim writes `claim_token` and returns claim handle fields:
  - `test_claim_delivery_targets_marks_rows_processing_and_uses_skip_locked`
  - `test_run_delivery_cycle_logs_claim_with_claim_token_and_passes_runtime`
- AC-2 / finalization owns post-claim mutation by `claim_token`, clears claim state, and reports ownership loss without mutation:
  - `test_load_claimed_processing_target_uses_claim_token_and_for_update`
  - `test_finalize_delivery_claim_returns_ownership_lost_without_mutation`
  - `test_deliver_claimed_target_marks_sent_on_success`
  - `test_deliver_claimed_target_retries_retryable_failures`
  - `test_deliver_claimed_target_dead_letters_when_target_is_missing_or_disabled`
  - `test_deliver_claimed_target_dead_letters_terminal_http_and_preserves_next_attempt`
  - `test_deliver_claimed_target_skips_sent_update_when_ownership_is_lost`
  - `test_deliver_claimed_target_skips_retry_update_when_ownership_is_lost`
  - `test_deliver_claimed_target_skips_dead_letter_update_when_ownership_is_lost`
  - `test_recover_stale_delivery_targets_preserves_attempt_count_and_clears_lock`
- AC-3 / executor decides retry exhaustion and repository does not reclassify:
  - `test_classify_delivery_attempt_builds_retryable_outcome_before_finalization`
  - `test_classify_delivery_attempt_converts_retry_exhaustion_before_finalization`
  - `test_finalize_delivery_claim_uses_supplied_retryable_outcome_without_recomputing`
  - `test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted`

## Preserved Invariants Checked

- Global invalid-config suppression skips session work and omits row-state fields:
  - `test_run_delivery_cycle_logs_invalid_config_suppression_without_row_state`
- `SLACK_ENABLED=false` skips stale-lock recovery and claim/send work:
  - `test_run_delivery_cycle_skips_disabled_slack_without_mutating_rows`
- Retryable errors remain sanitized and deterministic:
  - `test_deliver_claimed_target_redacts_urls_from_retryable_errors`
- Rendering and timeout behavior stay intact:
  - `test_render_slack_message_escapes_user_derived_fields`
  - `test_send_slack_webhook_enforces_total_timeout`

## Edge Cases / Failure Paths

- Missing or disabled target config at send time.
- Malformed payload before HTTP send.
- Retryable transport timeout vs retry exhaustion.
- Ownership lost on sent, failed, and dead-letter finalization paths.
- Stale-lock recovery clears `claim_token` while preserving `attempt_count`.

## Flake Controls

- All time-sensitive assertions use fixed `utc_now` monkeypatches.
- All DB and logging effects are isolated behind deterministic fake session and fake result objects.
- No test performs live network I/O; webhook calls are monkeypatched.

## Known Gaps

- No live SQLAlchemy integration test for `claim_token` persistence; current coverage is unit-level with statement inspection and fake DB objects.
- No multithreaded concurrency test for competing workers; skip-locked and ownership-lost behavior are covered through deterministic single-process fakes.
