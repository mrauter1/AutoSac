# Test Strategy

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: test
- Phase ID: async-delivery-runtime
- Phase Directory Key: async-delivery-runtime
- Phase Title: Async Delivery Runtime
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- AC-1 dedicated runtime loop and worker wiring:
  - `test_delivery_loop_runs_until_stop_event`
  - `test_start_slack_delivery_thread_wires_worker_instance_id`
- AC-2 delivery-state transitions and attempt handling:
  - `test_claim_delivery_targets_marks_rows_processing_and_uses_skip_locked`
  - `test_recover_stale_delivery_targets_preserves_attempt_count_and_clears_lock`
  - `test_deliver_claimed_target_marks_sent_on_success`
  - `test_deliver_claimed_target_retries_retryable_failures`
  - `test_deliver_claimed_target_dead_letters_terminal_http_and_preserves_next_attempt`
  - `test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted`
  - `test_deliver_claimed_target_dead_letters_when_target_is_missing_or_disabled`
  - `test_deliver_claimed_target_dead_letters_malformed_payload_without_http`
- AC-3 runtime suppression leaves rows untouched:
  - `test_run_delivery_cycle_logs_invalid_config_suppression_without_row_state`
  - `test_run_delivery_cycle_skips_disabled_slack_without_mutating_rows`

## Preserved invariants checked

- Slack rendering uses payload-only snapshots and escapes user-derived content:
  - `test_render_slack_message_escapes_user_derived_fields`
- Missing/disabled targets remain pre-send terminal failures with zero HTTP sends:
  - `test_deliver_claimed_target_dead_letters_when_target_is_missing_or_disabled`
- Malformed render payloads remain pre-send terminal failures with zero HTTP sends and preserved post-claim attempt counts:
  - `test_deliver_claimed_target_dead_letters_malformed_payload_without_http`
- Retry exhaustion preserves terminal semantics instead of rescheduling:
  - `test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted`

## Edge cases and failure paths

- Hard total webhook timeout:
  - `test_send_slack_webhook_enforces_total_timeout`
- Operator-facing error redaction for absolute URLs and Slack-hook fragments:
  - `test_sanitize_operator_summary_redacts_urls`
  - `test_deliver_claimed_target_redacts_urls_from_retryable_errors`
- Render-time terminal validation failures dead-letter without outbound HTTP and keep `next_attempt_at` unchanged:
  - `test_deliver_claimed_target_dead_letters_malformed_payload_without_http`
- Invalid-config suppression logs omit row-state payload when no row is claimed:
  - `test_run_delivery_cycle_logs_invalid_config_suppression_without_row_state`

## Reliability / stabilization

- Tests stay at the helper seam with fake DB/session objects; no real network, DB, or long-lived worker threads are required.
- Time-sensitive assertions use fixed `utc_now` monkeypatches instead of wall-clock sleeps for retry/dead-letter timestamps.
- The total-timeout test monkeypatches the async send coroutine to a deterministic `asyncio.sleep(...)` case, so the timeout branch is exercised without external I/O.
- Suppression-path tests monkeypatch `session_scope` and logging directly to assert that no claim/recovery work runs when Slack is disabled or globally invalid.

## Known gaps

- Coverage remains helper-level rather than end-to-end against a real PostgreSQL database or live Slack endpoint.
- No test currently exercises the sanitized cycle-error log payload directly; current coverage focuses on persisted `last_error` and suppression/transition logs.
