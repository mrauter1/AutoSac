# Test Author ↔ Test Auditor Feedback

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: test
- Phase ID: async-delivery-runtime
- Phase Directory Key: async-delivery-runtime
- Phase Title: Async Delivery Runtime
- Scope: phase-local authoritative verifier artifact

- Expanded `tests/test_slack_delivery.py` with two focused regression checks that were still missing from the runtime slice: one for the `SLACK_ENABLED=false` suppression path leaving rows untouched and one for URL redaction flowing through the actual retryable-error delivery path.
- Updated `test_strategy.md` with an explicit behavior-to-test map for AC-1 through AC-3, preserved invariants, failure-path coverage, stabilization tactics, and the remaining helper-level gaps.
- Focused validation remained green after the additions:
  - `pytest tests/test_slack_delivery.py tests/test_slack_event_emission.py`
  - `pytest tests/test_ai_worker.py -k 'heartbeat_loop_emits_while_stop_event_controls_exit or emit_worker_heartbeat_initializes_system_state_defaults or emit_worker_heartbeat_updates_active_run_last_heartbeat'`

- TST-001 | blocking | `tests/test_slack_delivery.py`
  The suite now covers pre-send terminal failure for missing/disabled targets, but it still does not cover the other render-time terminal path that the phase contract explicitly includes: malformed payloads or unsupported `event_type` values. Missed-regression scenario: a bad `payload_json` or unexpected event type could start making outbound HTTP requests, leave the row stuck in `processing`, or bypass the `dead_letter` path, and this test slice would still pass. Minimal fix: add a focused delivery test that seeds a claimed target with malformed render input, asserts `send_webhook` is never called, and verifies the row transitions directly to `dead_letter` with the post-claim `attempt_count` preserved.
- Addressed TST-001 with `test_deliver_claimed_target_dead_letters_malformed_payload_without_http`, which seeds a malformed `ticket.public_message_added` payload, asserts the send hook is never called, and verifies `dead_letter`, lock clearing, preserved post-claim `attempt_count`, preserved `next_attempt_at`, and a render-validation error summary.
- Validation for the added malformed-payload coverage stayed green:
  - `python3 -m compileall tests/test_slack_delivery.py`
  - `pytest tests/test_slack_delivery.py tests/test_slack_event_emission.py` (`31 passed`)
  - `pytest tests/test_ai_worker.py -k 'heartbeat_loop_emits_while_stop_event_controls_exit or emit_worker_heartbeat_initializes_system_state_defaults or emit_worker_heartbeat_updates_active_run_last_heartbeat'` (`3 passed`)
- Audit pass (cycle 2): rechecked the focused runtime and adjacent worker slices, verified `TST-001` is resolved by the new malformed-payload dead-letter test, and found no additional blocking or non-blocking coverage issues in the active phase scope.
