# Test Strategy

- Task ID: prd-phase-1-slack-outbound-notifications-for-aut-75cb0f46
- Pair: test
- Phase ID: transactional-event-emission
- Phase Directory Key: transactional-event-emission
- Phase Title: Transactional Event Emission
- Scope: phase-local producer artifact

## Behavior-to-test coverage map

- AC-1 eligible transactional emission:
  - `test_create_requester_ticket_emits_ticket_created_only`
  - `test_add_requester_reply_emits_public_message_and_status_changed`
  - `test_ticket_created_suppression_paths_record_event_and_links_without_target_row`
  - `test_invalid_config_emission_logs_suppression_without_row_state_fields`
- AC-2 initial creation exclusions:
  - `test_create_requester_ticket_emits_ticket_created_only`
- AC-3 duplicate reuse without target repair:
  - `test_duplicate_reuse_preserves_zero_target_state_and_log_after_routing_change`
  - `test_duplicate_reuse_preserves_existing_target_row_state_without_creating_second_row`

## Preserved invariants checked

- Internal notes stay non-emitting:
  - `test_add_ops_internal_note_creates_no_integration_rows`
- Worker/internal flows emit only status changes when required:
  - `test_create_ai_draft_emits_only_status_changed_for_worker_draft_creation`
  - `test_route_ticket_after_ai_emits_status_changed_without_public_message`
  - `test_ai_failure_note_flow_emits_status_changed_but_no_public_message`
- Published AI drafts use persisted public message authorship:
  - `test_publish_ai_draft_for_ops_uses_ai_public_message_author_in_payload`

## Edge cases and failure paths

- Message preview normalization and truncation:
  - `test_build_message_preview_normalizes_unicode_whitespace_and_truncates`
- Ticket URL normalization with or without trailing slash:
  - `test_build_ticket_created_payload_normalizes_trailing_slash_in_ticket_url`
- Emission-time suppression outcomes with zero target-row creation:
  - `suppressed_slack_disabled`
  - `suppressed_target_disabled`
  - `suppressed_notify_disabled`
  - `suppressed_invalid_config`
- Duplicate reuse preserves both previously zero-target and previously routed target-row state under later config changes.
- Invalid-config logs omit row-state fields when no target row exists:
  - `test_invalid_config_emission_logs_suppression_without_row_state_fields`

## Reliability / stabilization

- Tests use the fake-session seam already used by the repo instead of real DB or worker threads.
- Emission-query helpers are monkeypatched to deterministic in-memory maps, so duplicate and target-row assertions do not depend on transaction timing.
- Logging assertions capture structured payloads directly via `shared.integrations.log_event` monkeypatches; no log parsing or ordering beyond the local call under test.

## Known gaps

- Slack HTTP delivery, retries, stale-lock recovery, and rendered message escaping are intentionally deferred to later phases.
- These tests validate emission hooks and persisted snapshot shape at the shared helper/ticketing seam, not end-to-end worker delivery behavior.
