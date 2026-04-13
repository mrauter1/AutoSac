# Test Strategy

- Task ID: slack-implementation-refactor
- Pair: test
- Phase ID: worker-dm-delivery-and-regression
- Phase Directory Key: worker-dm-delivery-and-regression
- Phase Title: Worker DM Delivery and Regression Completion
- Scope: phase-local producer artifact

## Behavior-to-test map

- `AC-1` preflight suppression before row mutation:
  - `tests/test_slack_delivery.py::test_run_delivery_cycle_suppresses_on_auth_test_invalid_config_and_persists_health`
  - Confirms auth-level invalid config skips claim and stale-lock recovery and persists invalid-config health.
- `AC-1` send-time auth or scope invalid config leaves rows unchanged for the affected cycle:
  - `tests/test_slack_delivery.py::test_run_delivery_cycle_restores_unfinalized_claims_when_send_hits_missing_scope`
  - `tests/test_slack_delivery.py::test_run_delivery_cycle_keeps_earlier_sent_rows_finalized_when_later_send_hits_missing_scope`
  - Confirms unfinalized claims are restored, stale-lock recovery is skipped, and earlier rows finalized before the halt stay finalized.
- `AC-2` recipient resolution and DM success path:
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_marks_sent_on_success_and_uses_current_recipient_slack_id`
  - `tests/test_slack_delivery.py::test_classify_delivery_attempt_requires_ok_true_for_conversations_open`
  - `tests/test_slack_delivery.py::test_classify_delivery_attempt_requires_ok_true_for_chat_post_message`
- `AC-2` terminal recipient failures without Slack HTTP calls:
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_dead_letters_invalid_recipients_without_slack_calls`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_dead_letters_recipient_specific_slack_errors`
- Retry and backoff classification:
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_retries_transport_errors`
  - `tests/test_slack_delivery.py::test_classify_delivery_attempt_honors_retry_after_floor`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_dead_letters_when_retry_budget_is_exhausted`
- Worker observability:
  - `tests/test_slack_delivery.py::test_run_delivery_cycle_uses_db_loaded_runtime_and_logs_claim_with_recipient_fields`
  - `tests/test_slack_delivery.py::test_deliver_claimed_target_logs_ownership_lost_with_recipient_fields`
- Docs and contract hardening:
  - `tests/test_hardening_validation.py`
  - Validates the repo docs and examples describe one DB-backed Slack DM contract.

## Preserved invariants checked

- No request-path Slack HTTP dependency is introduced by the worker changes.
- Send-time invalid config does not finalize or recover delivery rows for the affected cycle.
- Stale-lock recovery still exists, but now runs only after a send pass completes without a global send-time invalid-config halt.

## Edge cases and failure paths

- `auth.test` invalid auth suppression before any row work.
- `conversations.open` `missing_scope` both as the first claimed send and after an earlier successful send in the same cycle.
- Missing or inactive recipients and missing Slack IDs.
- Retryable transport, timeout, `429`, and `5xx` paths plus retry exhaustion.

## Flake controls

- Worker-cycle tests patch `session_scope`, Slack Web API helpers, and recipient loading to avoid network, time, and DB nondeterminism.
- Mixed-batch tests use explicit per-row in-memory state and call counters instead of ordering by wall-clock side effects.

## Known gaps

- No live Slack Web API integration test is added; coverage stays at deterministic unit or orchestration level.
- No browser-level validation of the docs surfaces is added in this phase.
